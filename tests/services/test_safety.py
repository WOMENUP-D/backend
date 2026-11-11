"""Regression corpus for the safety gate (A-02, I-14).

Every phrase the QA audit found slipping through, more realistic variants
in both Uzbek Latin alphabets, Uzbek Cyrillic, Russian and English — and a
negative corpus of ordinary questions, so precision is held as firmly as
recall. Adding a lexicon rule means adding its phrases here.
"""

import pytest

from app.services.rag import needs_human_escalation
from app.services.safety import SafetyCategory, detect, mentions_safety_topic, normalise

DISCLOSURES = [
    "Erim meni urishadi",
    "Erim har kuni uryapti",
    "Meni urib ketdi",
    "Erim pulimni tortib oladi va uydan chiqarmaydi",
    "yashashni xohlamayman",
    "Меня бьют дома",
    "Жить не хочется",
    "Отчим трогает меня",
    "My husband beat me",
    "He hit me",
    "I want to hurt myself",
    "I want to end my life",
    "My boyfriend forces me to have sex",
    "Erim meni uradi, nima qilay?",
    "Erim meni uryapti",
    "Eram meni kaltakladi, nima qilay?",
    "Meni ishga majburlayapti va tahdid qilyapti",
    "Меня избивает муж",
    "Он мне угрожает",
    "Я не хочу жить",
    "My husband beats me",
    "I want to kill myself",
    "I want to die",
    "Oʻzimni oʻldirgim kelyapti",
    "O‘zimni o‘ldirgim kelyapti",
    "O'zimni o'ldirgim kelyapti",
    "Ozimni oldirgim kelyapti",
    "Yaşagim kelmayapti",
    "Erim meni urayapti va qörqityapti",
    "Özimni öldirmoqçiman",
    "Меня бьют дома, я боюсь идти на курсы",
    "My husband beat me yesterday and took my phone. Can I still study?",
    "Отчим трогает меня, когда мамы нет дома. Что мне делать?",
    "Erim meni kaltaklaydi",
    "Erim meni o'ldiraman deb qo'rqityapti",
    "Qaynonam meni urib, uydan chiqarmayapti",
    "O'gay otam menga tegadi",
    "Qo'shnimiz meni zo'rladi",
    "Meni majburan turmushga berishyapti",
    "Erim ishlashimga ruxsat bermaydi",
    "Erim pasportimni olib qo'ydi",
    "Эрим мени уради",
    "Ўзимни ўлдиргим келяпти",
    "Яшагим келмаяпти",
    "Эрим мени калтаклади",
    "Муж отбирает у меня зарплату и не выпускает из дома",
    "Хочу покончить с собой",
    "Меня изнасиловали",
    "Я режу себя",
    "Не хочу больше жить",
    "My stepfather touches me when mom is not home",
    "He takes my salary and won't let me leave the house",
    "I've been thinking about suicide",
    "My husband won't let me work",
    "I was raped last year",
    "ERIM MENI URADI!!!",
    "erim...meni...uradi",
    "Erim-meni-uryapti",
    "O'lgim kelyapti",
    "Öz joniga qasd qilmoqchiman",
    "Erim bolamni uradi",
    "My dad hits my little brother and me",
    "Uyda zoravonlik bo'lyapti, qonun nima deydi?",
    "Oiladagi zoravonlik",
    "I am being abused",
    "Men zoravonlikka uchradim",
    "He threatened to kill me",
    "Отец бьёт меня",
    "Он меня бил",
    "Муж душит меня",
]

ORDINARY = [
    "Kursni olib qoysam bo'ladimi? Keyin qaytadan boshlasam?",
    "Onlayn ishda firibgarlardan qanday saqlanaman?",
    "I love grapes",
    "Domestic violence course?",
    "Qaysi kursni tanlashim kerak?",
    "Как открыть ИП?",
    "Buxgalteriya kursiga qanday yozilaman?",
    "Grant olish uchun nima kerak?",
    "How do I get a certificate?",
    "Bu kursni olgim kelyapti",
    "Matematikada qiynalyapman",
    "Imtihondan qo'rqyapman",
    "Imtihon meni qo'rqitadi",
    "Kiberxavfsizlik tahdidlari haqida kurs bormi?",
    "I hit a wall in my studies, what next?",
    "Как бороться со стрессом на работе?",
    "Мама заставляет меня учиться на врача",
    "Ishga urinib ko'ryapman",
    "Zoravonlik haqida qonun qanday?",
    "Как защититься от мошенников в интернете?",
    "What are my rights if I am fired?",
    "Urush tarixi kursi bormi?",
    "Telefonimni qanday sozlayman?",
    "Uydan ishlash mumkinmi?",
    "Erim kursga yozilishimga yordam beradi",
    "Hayotimni o'zgartirmoqchiman",
    "Yashash uchun pul topish kerak",
    "Erim meni qo'llab-quvvatlaydi",
    "I want to dye my hair",
    "Pulimni qanday tejayman?",
    "Bolamni bog'chaga beraymi?",
    "Men kurs oldim va sertifikat oldim",
    "Ish izlayapman, yordam bering",
    "Turmush o'rtog'im menga kurs sovg'a qildi",
    "How do I deal with stress at work?",
    "Мне нужна помощь с резюме",
    "Kursni tugatdim, endi nima qilay?",
    "Erim urishqoq emas, lekin men ishlamoqchiman",
    " Bu ish meni charchatyapti",
    "What is harassment at work?",
    "Substance abuse prevention course",
    "Pul tejash uchun kartani olib qo'ydim",
    "Ertaga darsga boraman",
    "Ukam maktabda futbol o'ynaydi",
    "Hit songs for my presentation?",
    "Как начать бизнес дома?",
    "Онлайн-курсы для детей",
    "Yangi telefon olib berdi erim",
]


@pytest.mark.parametrize("message", DISCLOSURES)
def test_a_disclosure_is_caught(message):
    assert detect(message) is not None
    assert needs_human_escalation(message)


@pytest.mark.parametrize("message", ORDINARY)
def test_an_ordinary_question_is_not_a_disclosure(message):
    assert detect(message) is None
    assert not needs_human_escalation(message)


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("Ozimni oldirgim kelyapti", SafetyCategory.SELF_HARM),
        ("I want to end my life", SafetyCategory.SELF_HARM),
        ("Отчим трогает меня", SafetyCategory.CHILD_ABUSE),
        ("Erim bolamni uradi", SafetyCategory.CHILD_ABUSE),
        ("Qo'shnimiz meni zo'rladi", SafetyCategory.SEXUAL_VIOLENCE),
        ("My husband beat me", SafetyCategory.PHYSICAL_VIOLENCE),
        ("Erim pulimni tortib oladi va uydan chiqarmaydi", SafetyCategory.COERCIVE_CONTROL),
    ],
)
def test_the_category_is_the_kind_of_harm(message, category):
    assert detect(message) is category


def test_both_alphabets_cyrillic_and_punctuation_fold_to_one_form():
    forms = [
        "Oʻzimni oʻldirgim kelyapti",
        "Özimni öldirgim kelyapti",
        "Ўзимни ўлдиргим келяпти",
        "OZIMNI...OLDIRGIM,KELYAPTI!!!",
    ]
    assert {normalise(f).lat for f in forms} == {"ozimni oldirgim kelyapti"}


def test_the_soft_form_keeps_the_mark_that_separates_dying_from_taking():
    assert detect("O'lgim kelyapti") is SafetyCategory.SELF_HARM
    assert detect("Bu kursni olgim kelyapti") is None


def test_editorial_screening_stays_broad():
    # News about these subjects waits for an editor even when it is not a
    # first-person disclosure; fraud stories too.
    assert mentions_safety_topic("Domestic violence course?")
    assert mentions_safety_topic("Onlayn firibgarlar haqida")
    assert not mentions_safety_topic("Yangi buxgalteriya kursi ochildi")


def test_empty_input_is_not_a_disclosure():
    assert detect("") is None
    assert detect("   ") is None
    assert detect(None) is None
