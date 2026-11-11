"""The safety gate: disclosures of violence or self-harm, detected without a model.

A woman who writes that her husband beats her, that someone touches her child,
that she is kept at home without her money, or that she does not want to live,
must reach a person — and her words must never become a generative prompt.
This module is that boundary. Every AI entry point calls `detect` on the raw
message *before* routing, retrieval, quotas or any model call, and answers a
disclosure with `escalate`, which raises a coordinator signal for a signed-in
user and returns fixed, honest text with approved resources only.

How detection works
-------------------
1. **Normalise.** Case-fold; transliterate Uzbek Cyrillic; fold both Uzbek
   Latin alphabets to one form (ö/oʻ/o' → o, ğ/gʻ → g, ş → sh, ç → ch);
   strip every apostrophe glyph and punctuation. Russian keeps its own
   Cyrillic form (ё → е). A *soft* form keeps ö and ğ for the few Uzbek words
   whose meaning depends on them: "ölgim kelyapti" (I want to die) and
   "olgim kelyapti" (I want to take [the course]) differ only in that mark.
2. **Match stems, not phrases.** Each rule is a regular expression over word
   stems with their tense, person and number endings.
3. **Context where a stem alone is ambiguous.** "uradi" (beats) and "hits" are
   disclosures next to "meni"/"me" or a family member, and ordinary words
   elsewhere ("I hit a wall in my studies"). Those rules require a victim or
   family marker in the message.
4. **Topic words are not disclosures.** "Domestic violence course?" or
   "Zoʻravonlik haqida qonun" asks for information. A bare topic noun is
   escalated only when the message has a victim marker or is *not* an
   information request — missing a disclosure costs more than a false alarm.

Fraud ("firibgar", "мошенник") is deliberately not here: a question about
avoiding scams deserves an ordinary, informative answer, not a crisis reply.

This is keyword matching across three languages and two scripts. It is a floor
under the prompts, not a substitute for a person; the regression corpus in
`tests/services/test_safety.py` holds both what it must catch and what it must
let through, so precision is tested as well as recall.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    DataClassification,
    NotificationTrigger,
    RiskFlagType,
    Role,
    UserStatus,
)
from app.models.audit import RiskFlag
from app.models.user import User, UserRole
from app.services import notification_service
from app.services.audit_service import record_audit
from app.services.uzbek import to_new_alphabet

logger = logging.getLogger(__name__)


class SafetyCategory(StrEnum):
    """What kind of disclosure was detected. Stored instead of her words."""

    SELF_HARM = "self_harm"
    CHILD_ABUSE = "child_abuse"
    SEXUAL_VIOLENCE = "sexual_violence"
    PHYSICAL_VIOLENCE = "physical_violence"
    COERCIVE_CONTROL = "coercive_control"


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

_APOSTROPHES = "ʻʼ‘’'`´ʹʽ"

_UZ_CYRILLIC = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "ғ": "ğ",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "j",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "қ": "q",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "ў": "ö",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "x",
    "ҳ": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sh",
    "ъ": "",
    "ы": "i",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}
_UZ_CYRILLIC_TABLE = str.maketrans(_UZ_CYRILLIC)
_NON_WORD = re.compile(r"[^\w]+")
_UNDERSCORE = re.compile(r"_+")


def _squash(text: str) -> str:
    text = _UNDERSCORE.sub(" ", _NON_WORD.sub(" ", text))
    return " ".join(text.split())


@dataclass(frozen=True, slots=True)
class Normalised:
    """The three forms a message is matched in."""

    #: Latin, both Uzbek alphabets folded, no apostrophes: "ozimni oldirgim".
    lat: str
    #: Latin keeping ö and ğ, for the words whose meaning hangs on them.
    soft: str
    #: Cyrillic as typed (ё → е), for Russian.
    ru: str


def normalise(text: str) -> Normalised:
    lowered = (text or "").lower()

    ru = _squash(lowered.replace("ё", "е"))

    latin = lowered.translate(_UZ_CYRILLIC_TABLE)
    # oʻ / gʻ in any apostrophe glyph are the reformed ö / ğ.
    latin = re.sub(f"o[{_APOSTROPHES}]", "ö", latin)
    latin = re.sub(f"g[{_APOSTROPHES}]", "ğ", latin)
    latin = latin.replace("ş", "sh").replace("ç", "ch")
    for mark in _APOSTROPHES:
        latin = latin.replace(mark, "")
    soft = _squash(latin)
    lat = soft.replace("ö", "o").replace("ğ", "g")
    return Normalised(lat=lat, soft=soft, ru=ru)


# --------------------------------------------------------------------------
# Lexicons
# --------------------------------------------------------------------------


def _re(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


#: Someone the harm is done to, or a relative who does it. Required by the
#: rules whose stem alone is an ordinary word.
_VICTIM_LAT = _re(
    r"\b(meni|menga|mendan|bizni|bizga|ozimni|erim\w*|eri|turmush ortogim\w*|"
    r"qaynonam\w*|qaynotam\w*|qaynim\w*|qaynopam\w*|otam\w*|ogay\s+\w+|onam\w*|akam\w*|"
    r"ukam\w*|opam\w*|amakim\w*|togam\w*|sevgilim\w*|yigitim\w*|qoshnim\w*|bolam\w*|"
    r"bolalarim\w*|qizim\w*|oglim\w*|farzandim\w*|uyda|uyimda|oilamda|"
    r"me|us|myself|husband|boyfriend|stepfather|stepdad|stepmother|at home)\b"
)
_VICTIM_RU = _re(
    r"\b(меня|мне|мной|нас|нам|муж\w*|отчим\w*|отец|отца|папа|свекров\w*|свекор\w*|"
    r"брат\w*|парень|парня|сожител\w*|бывш\w*|дяд\w*|сосед\w*|ребен\w*|детей|дети|"
    r"сын\w*|доч\w*|дома|в семье)\b"
)
#: A family member or partner as the one acting — stricter than a victim
#: marker, for verbs like "qoʻrqitadi" that ordinary things do too ("the exam
#: scares me").
_ABUSER_LAT = _re(
    r"\b(erim\w*|eri|turmush ortogim\w*|qaynonam\w*|qaynotam\w*|qaynim\w*|otam\w*|"
    r"ogay\s+\w+|akam\w*|amakim\w*|togam\w*|sevgilim\w*|yigitim\w*|qoshni\w*|"
    r"husband|boyfriend|partner|stepfather|stepdad|uncle|ex|father in law|"
    r"mother in law|brother in law)\b"
)
#: A child involved, as victim or with a step-parent — moves the category.
_CHILD_LAT = _re(
    r"\b(bolam\w*|bolalarim\w*|qizim\w*|oglim\w*|farzandim\w*|ogay\s+(otam|onam)\w*|"
    r"kids?|child|children|son|daughter|baby|stepfather|stepdad)\b"
)
_CHILD_RU = _re(r"\b(ребен\w*|детей|дети|сын\w*|доч\w*|отчим\w*|мачех\w*)\b")

#: An information request — a topic word next to one of these is a question
#: about the subject, not a disclosure.
_INFO_LAT = _re(
    r"\b(kurs\w*|course\w*|dars\w*|lesson\w*|trening\w*|training\w*|qonun\w*|law\w*|"
    r"huquq\w*|rights?|maqola\w*|article\w*|statistik\w*|statistic\w*|malumot\w*|"
    r"information|mavzu\w*|topic\w*|loyiha\w*|project\w*|seminar\w*|webinar\w*|"
    r"vebinar\w*|kitob\w*|book\w*|film\w*|video\w*|prevention|oldini olish\w*|"
    r"research|tadqiqot\w*|what\s+is|nima\s+degani|nima\s+bu)\b"
)
_INFO_RU = _re(
    r"\b(курс\w*|урок\w*|тренинг\w*|закон\w*|права|право|стать\w*|статистик\w*|"
    r"информаци\w*|тема|тему|проект\w*|семинар\w*|вебинар\w*|книг\w*|фильм\w*|видео|"
    r"профилактик\w*|исследован\w*|что\s+такое)\b"
)

_EN_HIT_VERBS = (
    r"(beat|beats|beating|beaten|hit|hits|hitting|punch|punches|punched|punching|slap|"
    r"slaps|slapped|slapping|kick|kicks|kicked|kicking|choke|chokes|choked|choking|"
    r"strangle\w*|shove|shoves|shoved|hurt|hurts|hurting|abuse|abuses|abused|abusing)"
)
_EN_KIN = (
    r"(husband|boyfriend|partner|ex|father|stepfather|dad|stepdad|brother|uncle|"
    r"mother in law|father in law|brother in law)"
)


@dataclass(frozen=True, slots=True)
class _Rule:
    category: SafetyCategory
    pattern: re.Pattern[str]
    form: str = "lat"  # "lat" | "soft" | "ru"
    #: None, "victim" or "abuser" — the context the message must also contain.
    needs: str | None = None


def _rule(category, pattern, form="lat", needs=None) -> _Rule:
    return _Rule(category, _re(pattern), form, needs)


S = SafetyCategory

_RULES: tuple[_Rule, ...] = (
    # ------------------------------------------------------------ self-harm
    # uz
    _rule(S.SELF_HARM, r"\bozimni\s+(\w+\s+)?(oldir|osib|osmoqchi|kes|zaharla|tashla|yoq qil)\w*"),
    _rule(S.SELF_HARM, r"\b(oz|ozim)\s+jon\w*\s+qasd\w*"),
    _rule(S.SELF_HARM, r"\bjonimga\s+qasd\w*"),
    _rule(S.SELF_HARM, r"\b(jonimni|hayotimni|umrimni)\s+(\w+\s+)?(tugat|yakunla|olib qoy)\w*"),
    _rule(
        S.SELF_HARM,
        r"\byasha(gim|shni|shga|sh)\s+(\w+\s+)?(kelma|xohlama|xohla\s*ma|istama)\w*",
    ),
    _rule(S.SELF_HARM, r"\byashashdan\s+(charcha|toy|bez|ozdim)\w*"),
    _rule(S.SELF_HARM, r"\byashashning\s+(manosi|mazmuni)\s+(yoq|qolmadi)\b"),
    _rule(S.SELF_HARM, r"\bozimga\s+(ozim\s+)?(jarohat|zarar|ziyon)\w*"),
    _rule(S.SELF_HARM, r"\bozimni\s+(jarohatla|kesyap|kesaman|kesdim)\w*"),
    _rule(S.SELF_HARM, r"\bölgim\s+kel\w*", form="soft"),
    _rule(S.SELF_HARM, r"\bölmoqchi(man)?\b", form="soft"),
    _rule(S.SELF_HARM, r"\bölib\s+qolsam\b", form="soft"),
    # ru
    _rule(S.SELF_HARM, r"\bпокончи\w*\s+с\s+собой\b", form="ru"),
    _rule(S.SELF_HARM, r"\b(суицид\w*|самоубийств\w*|самоповрежд\w*)", form="ru"),
    _rule(S.SELF_HARM, r"\bне\s+(хочу|хочется|хочеться)\s+(больше\s+)?жить\b", form="ru"),
    _rule(S.SELF_HARM, r"\bжить\s+(больше\s+)?не\s+(хочу|хочется|хочеться)\b", form="ru"),
    _rule(S.SELF_HARM, r"\bхочу\s+умереть\b", form="ru"),
    _rule(S.SELF_HARM, r"\b(убить|убью)\s+себя\b", form="ru"),
    _rule(S.SELF_HARM, r"\b(порезать|режу|резать|порезала|порезал)\s+себя\b", form="ru"),
    _rule(S.SELF_HARM, r"\b(причин\w*|наношу|нанести)\s+себе\s+(боль|вред|травм)\w*", form="ru"),
    _rule(S.SELF_HARM, r"\bнавредить\s+себе\b", form="ru"),
    _rule(S.SELF_HARM, r"\bнет\s+смысла\s+жить\b", form="ru"),
    _rule(S.SELF_HARM, r"\bлучше\s+бы\s+я\s+умер\w*", form="ru"),
    _rule(S.SELF_HARM, r"\bсвести\s+сч[её]ты\s+с\s+жизнью\b", form="ru"),
    # en
    _rule(S.SELF_HARM, r"\bkill(ing)?\s+myself\b"),
    _rule(S.SELF_HARM, r"\bend(ing)?\s+(my\s+(own\s+)?life|it\s+all)\b"),
    _rule(S.SELF_HARM, r"\btake\s+my\s+(own\s+)?life\b"),
    _rule(S.SELF_HARM, r"\b(hurt|hurting|harm|harming|cut|cutting)\s+myself\b"),
    _rule(S.SELF_HARM, r"\bself\s*harm\w*"),
    _rule(S.SELF_HARM, r"\bsuicid\w*"),
    _rule(S.SELF_HARM, r"\b(want|wanna|going)\s+(to\s+)?die\b(?!\s+(my|her|the)\s+hair)"),
    _rule(S.SELF_HARM, r"\bwish\s+i\s+(was|were)\s+dead\b"),
    _rule(S.SELF_HARM, r"\b(dont|do not)\s+want\s+to\s+(live|be alive|exist)\b"),
    _rule(S.SELF_HARM, r"\bno\s+(reason|point)\s+(to|in)\s+(live|living)\b"),
    _rule(S.SELF_HARM, r"\bbetter\s+off\s+dead\b"),
    # ------------------------------------------------------ sexual violence
    # uz
    _rule(S.SEXUAL_VIOLENCE, r"\bjinsiy\s+(tazyiq|zorav|tajovuz|aloqaga\s+majbur|shilqim)\w*"),
    _rule(
        S.SEXUAL_VIOLENCE,
        r"\bzorla(di|ydi|yapti|moqchi|b|shdi|shmoqchi|gan|ndim|ndi|nganman|shadi|shyapti)\b",
    ),
    _rule(S.SEXUAL_VIOLENCE, r"\bnomus\w*\s+teg\w*"),
    _rule(
        S.SEXUAL_VIOLENCE,
        r"\b(badanim|tanam|koksim|ichki kiyim)\w*\s+(\w+\s+){0,3}(teg|ushla|silay|siypala)\w*",
    ),
    _rule(
        S.SEXUAL_VIOLENCE,
        r"\b(teg(adi|yapti|di|ib|ishadi|moqda|ar|averadi)|ushlay(di|veradi)|"
        r"silay(di|veradi|apti)|siypala\w*|shilqimlik\w*)\b",
        needs="abuser",
    ),
    # ru
    _rule(S.SEXUAL_VIOLENCE, r"\bизнасил\w*", form="ru"),
    _rule(S.SEXUAL_VIOLENCE, r"\bнасилу(ет|ют|ешь)\b", form="ru"),
    _rule(S.SEXUAL_VIOLENCE, r"\bдомога\w*", form="ru"),
    _rule(
        S.SEXUAL_VIOLENCE,
        r"\b(принужда|заставля)\w*\s+(\w+\s+){0,3}(к\s+)?(секс\w*|интим\w*|близост\w*|спать\s+с)",
        form="ru",
    ),
    _rule(
        S.SEXUAL_VIOLENCE,
        r"\b(трога(ет|ют|л|ла|ли)|лапа(ет|ют|л)|приста[её]т|пристают|приставал\w*)\b",
        form="ru",
        needs="victim",
    ),
    # en
    _rule(S.SEXUAL_VIOLENCE, r"\b(raped|rapes|raping|molest\w*)\s+(me|us|my)\b"),
    _rule(
        S.SEXUAL_VIOLENCE,
        r"\b(i\s+was|i\s+got|been|being|was)\s+(\w+\s+)?(raped|molested|sexually\s+\w+)\b",
    ),
    _rule(S.SEXUAL_VIOLENCE, r"\bmolest\w*"),
    _rule(
        S.SEXUAL_VIOLENCE,
        r"\b(forc|pressur|mak|made)\w*\s+me\s+(\w+\s+){0,2}(have\s+sex|sleep\s+with|sex|"
        r"sexual|undress|send\s+nudes)",
    ),
    _rule(
        S.SEXUAL_VIOLENCE,
        r"\btouch(es|ed|ing)?\s+(me|my\s+(body|daughter|son|child|kids?))\b",
        needs="abuser",
    ),
    _rule(
        S.SEXUAL_VIOLENCE,
        r"\btouch(es|ed|ing)?\s+me\s+(\w+\s+)?(inappropriately|there|at night)",
    ),
    # ---------------------------------------------------- physical violence
    # uz
    _rule(S.PHYSICAL_VIOLENCE, r"\bkaltak(la|lay|lan)\w*"),
    _rule(S.PHYSICAL_VIOLENCE, r"\bdopp?[ao]sla\w*"),
    _rule(S.PHYSICAL_VIOLENCE, r"\b(urib|kaltaklab)\s+(ketdi|tashladi|yubordi|oldi|qoydi)\b"),
    _rule(S.PHYSICAL_VIOLENCE, r"\b(sochimdan|sochidan)\s+(\w+\s+)?tort\w*"),
    _rule(
        S.PHYSICAL_VIOLENCE,
        r"\bur(adi|adilar|di|dilar|yapti|ayapti|yaptilar|ib|ibdi|gan|moqda|ar|averadi|ishadi|"
        r"ishdi|ishyapti|ishib|ishgan|asan|ishar|aman|adimi|dimi)\b",
        needs="victim",
    ),
    _rule(S.PHYSICAL_VIOLENCE, r"\btep(adi|di|yapti|ib|gan|ishadi)\b", needs="victim"),
    _rule(S.PHYSICAL_VIOLENCE, r"\bbog(adi|di|ib|moqchi)\b", needs="victim"),
    _rule(S.PHYSICAL_VIOLENCE, r"\bqiyna(ydi|yapti|di|b|shadi|moqda)\b", needs="victim"),
    # ru
    _rule(
        S.PHYSICAL_VIOLENCE, r"\b(избива\w*|избил\w*|избит\w*|побои|побил\w*|поколотил\w*)", "ru"
    ),
    _rule(
        S.PHYSICAL_VIOLENCE,
        r"\b(бь(ет|ют|ешь)|бил|била|били|ударил\w*|ударяет|колотит|душит|душил\w*|толкает|"
        r"пинает|пинал\w*|таскает\s+за\s+волосы)\b",
        form="ru",
        needs="victim",
    ),
    # en
    _rule(
        S.PHYSICAL_VIOLENCE,
        rf"\b{_EN_HIT_VERBS}\b(\s+\w+){{0,2}}?\s+(me|us|myself|my\s+(kids?|children|child|son|"
        r"daughter|baby))\b",
    ),
    _rule(S.PHYSICAL_VIOLENCE, rf"\b{_EN_KIN}\b(\s+\w+){{0,3}}?\s+{_EN_HIT_VERBS}\b"),
    _rule(
        S.PHYSICAL_VIOLENCE,
        r"\b(will|gonna|going\s+to|threatens?\s+to|threatened\s+to|said\s+he\s+would)\s+"
        r"(kill|hurt|beat)\s+(me|us|my)\b",
    ),
    # ---------------------------------------------------- coercive control
    # uz
    _rule(S.COERCIVE_CONTROL, r"\b(majburan|zorlab)\b"),
    _rule(
        S.COERCIVE_CONTROL,
        r"\b(pul|maosh|oylig|hujjat|pasport|telefon|karta|nafaqa|daromad)\w*im\w*\s+(\w+\s+)?"
        r"(tortib|olib)\s+(oladi|oldi|olyapti|olishadi|olishdi|qoydi|qoyadi|qoyishdi|qoyyapti|"
        r"qoyishadi)\b",
    ),
    _rule(
        S.COERCIVE_CONTROL,
        r"\btortib\s+(oladi|oldi|olyapti|olishadi|olishdi)\b",
        needs="victim",
    ),
    _rule(
        S.COERCIVE_CONTROL,
        r"\b(uydan|kochaga|tashqariga|ishga|oqishga|kursga|ishlashga|oqishimga|ishlashimga|"
        r"ishlashim|oqishim)\w*\s+(chiqar|qoy|yubor|ruxsat\s+ber)ma(ydi|yapti|di|ydilar|"
        r"yaptilar|shadi|yishadi)\b",
        needs="victim",
    ),
    _rule(
        S.COERCIVE_CONTROL,
        r"\bmajburla(yapti|ydi|di|moqda|b|shadi|shyapti|gan|ydilar)\w*",
        needs="victim",
    ),
    _rule(S.COERCIVE_CONTROL, r"\btahdid\s+qil(yapti|adi|di|moqda|ib|ishadi|ishyapti)\b"),
    _rule(S.COERCIVE_CONTROL, r"\bqorqit(yapti|adi|di|ib|moqda|ishadi)\b", needs="abuser"),
    _rule(
        S.COERCIVE_CONTROL,
        r"\boldir(aman|amiz|ishadi|ib\s+yuboraman|moqchi|aman\s+deydi)\b",
        needs="victim",
    ),
    # ru
    _rule(
        S.COERCIVE_CONTROL,
        r"\b(отбира|забира|отним|отнял|отобрал)\w*\s+(\w+\s+){0,3}(деньг|зарплат|пенси|паспорт|"
        r"документ|телефон|карт)\w*",
        form="ru",
    ),
    _rule(
        S.COERCIVE_CONTROL,
        r"\bне\s+(выпуска|пуска|отпуска)\w*\s+(\w+\s+){0,2}(из\s+дома|на\s+работу|на\s+учебу|"
        r"на\s+курсы|никуда|на\s+улицу)",
        form="ru",
    ),
    _rule(
        S.COERCIVE_CONTROL,
        r"\b(угрожа\w*|запира\w*|запер\w*|принужда\w*)\b",
        form="ru",
        needs="victim",
    ),
    _rule(S.COERCIVE_CONTROL, r"\b(убь(ет|ю)|грозит\w*\s+убить)\b", form="ru"),
    # en
    _rule(
        S.COERCIVE_CONTROL,
        r"\b(takes?|took|taking|keeps?|kept)\s+(away\s+)?my\s+(money|salary|wages|pay|passport|"
        r"documents|papers|phone|id|card|pension)\b",
    ),
    _rule(
        S.COERCIVE_CONTROL,
        r"\b(wont|doesnt|does not|will not|never|dont|do not)\s+let\s+me\s+(leave|go|work|study|"
        r"see|out|have)\b",
        needs="abuser",
    ),
    _rule(S.COERCIVE_CONTROL, r"\block(s|ed)?\s+me\s+(in|up)\b"),
    _rule(S.COERCIVE_CONTROL, r"\bthreaten\w*\s+(me|to\s+kill|to\s+hurt|my)\b"),
    _rule(S.COERCIVE_CONTROL, r"\bforc\w*\s+(me\s+)?(to\s+)?marr\w*"),
)

#: Subject words. A disclosure when the message also has a victim marker or is
#: not an information request; a question about the subject otherwise.
_TOPICS: tuple[_Rule, ...] = (
    _rule(S.PHYSICAL_VIOLENCE, r"\b(zoravon\w*|tazyiq\w*|tajovuz\w*)"),
    _rule(
        S.PHYSICAL_VIOLENCE,
        r"\b(domestic\s+(violence|abuse)|violence|violent|abus\w*|battered)\b",
    ),
    _rule(S.SEXUAL_VIOLENCE, r"\b(rape|raped|sexual\s+(abuse|assault|harassment)|harassment)\b"),
    _rule(S.PHYSICAL_VIOLENCE, r"\b(насили\w*|абьюз\w*|побо(и|ев|ями))\b", form="ru"),
    _rule(S.SEXUAL_VIOLENCE, r"\b(харассмент\w*|сексуальн\w*\s+(насили|домога)\w*)", form="ru"),
)

#: Older fixed keywords, used only for screening *editorial* material (news),
#: where any mention of these subjects goes to an editor rather than being
#: published automatically. Not used on what a user asks.
_EDITORIAL_TOPICS = _re(
    r"\b(zoravon\w*|kaltak\w*|tajovuz\w*|tazyiq\w*|firibgar\w*|violence|abuse\w*|rape\w*|"
    r"suicid\w*|self\s*harm|trafficking|odam\s+savdosi)\b"
)
_EDITORIAL_TOPICS_RU = _re(
    r"\b(насили\w*|избие\w*|изнасил\w*|суицид\w*|мошенни\w*|торговл\w*\s+людьми)"
)


def _text_for(form: str, norm: Normalised) -> str:
    return norm.soft if form == "soft" else norm.ru if form == "ru" else norm.lat


def _has(needs: str | None, form: str, norm: Normalised) -> bool:
    if needs is None:
        return True
    if needs == "abuser":
        return bool(_ABUSER_LAT.search(norm.lat)) or (
            form == "ru" and bool(_VICTIM_RU.search(norm.ru))
        )
    return bool(_VICTIM_LAT.search(norm.lat) or _VICTIM_RU.search(norm.ru))


def _involves_child(norm: Normalised) -> bool:
    return bool(_CHILD_LAT.search(norm.lat) or _CHILD_RU.search(norm.ru))


def detect(text: str | None) -> SafetyCategory | None:
    """The disclosure category in `text`, or None. Pure and model-free."""
    if not text or not text.strip():
        return None
    norm = normalise(text)

    found: SafetyCategory | None = None
    for rule in _RULES:
        if rule.pattern.search(_text_for(rule.form, norm)) and _has(rule.needs, rule.form, norm):
            found = rule.category
            break

    if found is None:
        for rule in _TOPICS:
            if not rule.pattern.search(_text_for(rule.form, norm)):
                continue
            victim = _has("victim", rule.form, norm)
            informational = bool(_INFO_LAT.search(norm.lat) or _INFO_RU.search(norm.ru))
            if victim or not informational:
                found = rule.category
                break

    if found is None:
        return None
    # Self-harm stays self-harm; violence with a child in it is child abuse.
    if found in (S.PHYSICAL_VIOLENCE, S.SEXUAL_VIOLENCE) and _involves_child(norm):
        return S.CHILD_ABUSE
    return found


def is_disclosure(*texts: str | None) -> bool:
    return any(detect(text) is not None for text in texts)


def mentions_safety_topic(text: str | None) -> bool:
    """Broad screen for editorial material: a disclosure, or any mention of
    the subjects. Over-triggering here costs a post that waits for an editor."""
    if not text:
        return False
    if detect(text) is not None:
        return True
    norm = normalise(text)
    return bool(_EDITORIAL_TOPICS.search(norm.lat) or _EDITORIAL_TOPICS_RU.search(norm.ru))


def redacted(category: SafetyCategory) -> str:
    """What the trace keeps instead of her words (I-16)."""
    return f"[safety:{category.value}]"


# --------------------------------------------------------------------------
# What she is told, and the resources
# --------------------------------------------------------------------------

#: Approved resources. Only numbers already used by the portal's approved
#: texts appear here — nothing is invented. Add a line only once its number
#: has been confirmed by the content owner.
RESOURCES: tuple[dict, ...] = (
    {
        "id": "trust_line_1146",
        "phone": "1146",
        "tel": "tel:1146",
        "label": {
            "uz": to_new_alphabet("Ishonch telefoni"),
            "ru": "Телефон доверия",
            "en": "Helpline",
        },
    },
)


def resources(language: str) -> list[dict]:
    lang = language if language in ("uz", "ru", "en") else "uz"
    return [
        {"id": r["id"], "phone": r["phone"], "tel": r["tel"], "label": r["label"][lang]}
        for r in RESOURCES
    ]


def _uz(text: str) -> str:
    return to_new_alphabet(text)


# The opening line differs by category; the rest is shared. Every version is
# honest about what happened: a signed-in woman is told a WomanUP specialist
# has been alerted (true — `escalate` raised the flag), a guest is told no one
# has been, because there is no account anyone could reach.
_OPENING = {
    S.SELF_HARM: {
        "uz": _uz(
            "Siz buni yozganingiz yaxshi. Siz yolg'iz emassiz, va hozir siz bilan "
            "tirik odam gaplashishi muhim."
        ),
        "ru": (
            "Хорошо, что вы об этом написали. Вы не одна, и сейчас важно, чтобы с вами "
            "поговорил живой человек."
        ),
        "en": (
            "Thank you for telling me. You are not alone, and right now it matters that "
            "a real person talks with you."
        ),
    },
    "default": {
        "uz": _uz(
            "Siz bilan sodir bo'layotgan narsa jiddiy, va bunda siz aybdor emassiz. "
            "Bu haqda tirik mutaxassis yordam berishi kerak — men avtomatik yordamchiman."
        ),
        "ru": (
            "То, что с вами происходит, серьёзно, и вы в этом не виноваты. Здесь должен "
            "помочь живой специалист — я автоматический помощник."
        ),
        "en": (
            "What is happening to you is serious, and it is not your fault. A real "
            "specialist should help with this — I am an automated assistant."
        ),
    },
}

_FLAGGED = {
    "uz": _uz(
        "WomanUP mas'ul xodimiga xabar berildi; u portal orqali siz bilan bog'lanishi mumkin."
    ),
    "ru": (
        "Ответственный сотрудник WomanUP получил уведомление и может связаться с вами через портал."
    ),
    "en": "A WomanUP staff member has been notified and may contact you through the portal.",
}

_GUEST = {
    "uz": _uz(
        "Siz tizimga kirmagansiz, shuning uchun bu xabar hech kimga yuborilmadi. "
        "Iltimos, quyidagi telefon orqali tirik odam bilan bog'laning."
    ),
    "ru": (
        "Вы не вошли в аккаунт, поэтому это сообщение никому не передано. Пожалуйста, "
        "свяжитесь с живым человеком по телефону ниже."
    ),
    "en": (
        "You are not signed in, so this message has not been passed to anyone. Please "
        "reach a real person through the number below."
    ),
}

_DANGER = {
    "uz": _uz(
        "Agar hozir xavf ostida bo'lsangiz, 1146 ishonch telefoniga qo'ng'iroq qiling "
        "yoki yaqiningizdagi ishonchli odamdan yordam so'rang."
    ),
    "ru": (
        "Если вы сейчас в опасности, позвоните на телефон доверия 1146 или попросите "
        "о помощи человека, которому доверяете, рядом с вами."
    ),
    "en": (
        "If you are in danger now, call the 1146 helpline or ask someone you trust nearby for help."
    ),
}

_MINOR = {
    "uz": _uz("Ota-onangizdan biri, o'qituvchingiz yoki boshqa ishonchli kattaga ham ayting."),
    "ru": "Расскажите также родителю, учителю или другому взрослому, которому доверяете.",
    "en": "Please also tell a parent, a teacher or another adult you trust.",
}


def safety_text(
    category: SafetyCategory, language: str, *, flagged: bool, minor: bool = False
) -> str:
    lang = language if language in ("uz", "ru", "en") else "uz"
    opening = _OPENING.get(category, _OPENING["default"])[lang]
    middle = _FLAGGED[lang] if flagged else _GUEST[lang]
    parts = [opening, middle, _DANGER[lang]]
    if minor:
        parts.append(_MINOR[lang])
    return " ".join(parts)


#: For a model's own "needs a human" outside the safety filter — a question
#: the portal cannot answer, such as a request for someone else's data. Not a
#: crisis text, and no claim that anything was forwarded.
HANDOFF_TEXT = {
    "uz": _uz(
        "Bu savolga men javob bera olmayman — buni WomanUP xodimi hal qilishi kerak. "
        "Iltimos, hududingiz koordinatoriga murojaat qiling."
    ),
    "ru": (
        "На этот вопрос я ответить не могу — его должен решить сотрудник WomanUP. "
        "Пожалуйста, обратитесь к координатору вашего региона."
    ),
    "en": (
        "I cannot answer this one — a WomanUP staff member needs to handle it. "
        "Please contact your regional coordinator."
    ),
}


# --------------------------------------------------------------------------
# Escalation
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SafetyOutcome:
    category: SafetyCategory
    text: str
    flagged: bool
    risk_flag_id: uuid.UUID | None = None
    resources: list[dict] = field(default_factory=list)


async def _recipients(session: AsyncSession, user: User) -> list[User]:
    """Who hears about it: the coordinators of her region, and the national
    administrators when her region has none (or she has no region), so a
    signal is never raised into an empty room."""
    staff: list[User] = []
    if user.region is not None:
        staff = list(
            (
                await session.execute(
                    select(User)
                    .join(UserRole, UserRole.user_id == User.id)
                    .where(
                        UserRole.role == Role.REGIONAL_COORDINATOR,
                        UserRole.scope_region == user.region,
                        User.status == UserStatus.ACTIVE,
                        User.id != user.id,
                    )
                    .limit(10)
                )
            ).scalars()
        )
    if not staff:
        staff = list(
            (
                await session.execute(
                    select(User)
                    .join(UserRole, UserRole.user_id == User.id)
                    .where(
                        UserRole.role == Role.ADMIN,
                        User.status == UserStatus.ACTIVE,
                        User.id != user.id,
                    )
                    .limit(10)
                )
            ).scalars()
        )
    return list({member.id: member for member in staff}.values())


async def escalate(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    category: SafetyCategory,
    surface: str,
    language: str,
    trace_id: str,
    minor: bool = False,
) -> SafetyOutcome:
    """Answer a disclosure: a coordinator signal for a signed-in woman, honest
    text and approved resources for everyone.

    The flag never restricts her access to anything — it tells a person. It
    carries the category and the surface, never her words. One open flag per
    woman: a second disclosure while the first is unresolved is audited but
    does not page the coordinator again.
    """
    lang = language if language in ("uz", "ru", "en") else "uz"
    user = await session.get(User, user_id) if user_id is not None else None
    if user is None:
        return SafetyOutcome(
            category=category,
            text=safety_text(category, lang, flagged=False, minor=minor),
            flagged=False,
            resources=resources(lang),
        )

    flag = await session.scalar(
        select(RiskFlag).where(
            RiskFlag.user_id == user.id,
            RiskFlag.flag_type == RiskFlagType.SAFETY_ESCALATION,
            RiskFlag.resolved_at.is_(None),
        )
    )
    raised = flag is None
    if raised:
        recipients = await _recipients(session, user)
        flag = RiskFlag(
            user_id=user.id,
            flag_type=RiskFlagType.SAFETY_ESCALATION,
            severity="high",
            reason=category.value,
            evidence={"category": category.value, "surface": surface, "trace_id": trace_id},
            notified_coordinator_id=recipients[0].id if recipients else None,
        )
        session.add(flag)
        await session.flush()
        for member in recipients:
            await notification_service.enqueue(
                session,
                user_id=member.id,
                trigger=NotificationTrigger.SAFETY_ESCALATION,
                language=member.language.value if member.language else "uz",
                action_url="/admin",
                payload={"risk_flag_id": str(flag.id), "category": category.value},
            )
        if not recipients:
            logger.warning("safety flag raised with no coordinator or admin to notify")

    await record_audit(
        session,
        action="safety.escalation",
        entity_type="risk_flag",
        entity_id=str(flag.id),
        actor_id=user.id,
        classification=DataClassification.SENSITIVE,
        changes={
            "category": category.value,
            "surface": surface,
            "trace_id": trace_id,
            "new_flag": raised,
        },
    )
    await session.flush()
    return SafetyOutcome(
        category=category,
        text=safety_text(category, lang, flagged=True, minor=minor),
        flagged=True,
        risk_flag_id=flag.id,
        resources=resources(lang),
    )


__all__ = [
    "HANDOFF_TEXT",
    "RESOURCES",
    "SafetyCategory",
    "SafetyOutcome",
    "detect",
    "escalate",
    "is_disclosure",
    "mentions_safety_topic",
    "normalise",
    "redacted",
    "resources",
    "safety_text",
]
