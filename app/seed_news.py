"""The starting feed: what a woman reads on the day she registers.

Kept out of `seed.py` for the same reason `seed_modules.py` is — this is copy,
not logic, and it is long.

Two editorial rules run through everything below and should survive any later
edit. Every post about medicine names its source, because an unsourced health
claim on a state portal is a rumour with a government logo on it. And nothing
here diagnoses, prescribes or promises: the posts explain what is known and
send the reader to a doctor for what is hers.

`adult` marks a post that belongs to adult care. The feed withholds those from
a minor and from a reader whose age is not known — see `services.age_gate`.

Nothing here carries hand-written age scores. Each post is read by the keyword
rules in `services.news_age` as it is written, which is the same reading the
feed would apply to it anyway — so the demo feed personalises correctly out of
the box, and running the AI editor over it later sharpens the same numbers
rather than introducing them.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import NewsCategory
from app.models.news import NewsPost
from app.services.news_ai import apply_analysis, rule_based

logger = logging.getLogger(__name__)


def _tri(value: tuple[str, str, str]) -> dict[str, str]:
    """(uz, ru, en) -> the i18n map the API and the frontend both expect."""
    return {"uz": value[0], "ru": value[1], "en": value[2]}


# (slug, category, tone, emblem, tags, source_name, source_url, minutes,
#  adult, pinned, days_ago, title(uz, ru, en), summary(uz, ru, en),
#  body(uz, ru, en) — paragraphs joined by a blank line)
POSTS: list[dict] = [
    # ---- Announcements ------------------------------------------------
    {
        "slug": "womanup-yangiliklar-lentasi-ochildi",
        "category": NewsCategory.ANNOUNCEMENT,
        "tone": "plum",
        "emblem": "📣",
        "tags": ["womanup", "platforma"],
        "source": "WomanUP",
        "url": None,
        "minutes": 1,
        "adult": False,
        "pinned": True,
        "days_ago": 0,
        "title": (
            "WomanUP yangiliklar lentasi ochildi",
            "Открылась лента новостей WomanUP",
            "The WomanUP news feed is open",
        ),
        "summary": (
            "Tibbiyot, salomatlik, ayollarning ilmiy kashfiyotlari va eʼlonlar — hammasi bitta "
            "joyda.",
            "Медицина, здоровье, научные открытия женщин и объявления — всё в одном месте.",
            "Medicine, health, women's discoveries in science and announcements — all in one "
            "place.",
        ),
        "body": (
            "Bugundan boshlab portal kabinet bilan emas, lenta bilan ochiladi. "
            "Bu yerda siz uchun tanlangan yangiliklar chiqadi: tibbiyot nimani "
            "bilib oldi, ayollar fanda nimani kashf qildi, qaysi grant va "
            "dastur ochildi.\n\n"
            "Har bir tibbiy post manbasi bilan chiqadi — Jahon sogʻliqni saqlash "
            "tashkiloti, Nobel qoʻmitasi, UNESCO. Bu tasodif emas: manbasiz "
            "sogʻliq haqidagi daʼvo — bu shunchaki mish-mish.\n\n"
            "Lenta hech qachon tashxis qoʻymaydi va dori tavsiya qilmaydi. U "
            "nima maʼlum ekanini tushuntiradi va shifokorga yoʻnaltiradi.",
            "С сегодняшнего дня портал открывается не кабинетом, а лентой. "
            "Здесь выходят новости, отобранные для вас: что узнала медицина, "
            "что открыли женщины в науке, какой грант или программа открылись.\n\n"
            "Каждый медицинский пост выходит с источником — Всемирная "
            "организация здравоохранения, Нобелевский комитет, ЮНЕСКО. Это не "
            "формальность: утверждение о здоровье без источника — это просто слух.\n\n"
            "Лента никогда не ставит диагноз и не назначает лечение. Она "
            "объясняет, что известно, и направляет к врачу.",
            "From today the portal opens on the feed rather than on your "
            "cabinet. Here you will find news chosen for you: what medicine has "
            "learned, what women have discovered in science, which grant or "
            "programme has opened.\n\n"
            "Every medical post carries its source — the World Health "
            "Organization, the Nobel Committee, UNESCO. That is not a "
            "formality: a health claim with no source behind it is a rumour.\n\n"
            "The feed never diagnoses and never prescribes. It explains what is "
            "known and points you to a doctor.",
        ),
    },
    {
        "slug": "rivojlanish-balini-oting",
        "category": NewsCategory.ANNOUNCEMENT,
        "tone": "sand",
        "emblem": "🧭",
        "tags": ["diagnostika", "womanup"],
        "source": "WomanUP",
        "url": None,
        "minutes": 2,
        "adult": False,
        "pinned": True,
        "days_ago": 1,
        "title": (
            "Rivojlanish balini oʻtkazing — 8 yoʻnalish, 15 daqiqa",
            "Пройдите Развитие-балл — 8 направлений, 15 минут",
            "Take the Development Score — 8 dimensions, 15 minutes",
        ),
        "summary": (
            "Diagnostika taʼlim, ish, tadbirkorlik, moliya, sogʻliq, oila, ijtimoiy faollik va "
            "xalqaro integratsiyani oʻlchaydi.",
            "Диагностика измеряет образование, работу, предпринимательство, финансы, здоровье, "
            "семью, социальную активность и международную интеграцию.",
            "The assessment measures education, work, entrepreneurship, finance, health, family, "
            "social activity and international integration.",
        ),
        "body": (
            "Rivojlanish bali — bu imtihon emas. Bu sizning bugungi holatingizning "
            "surati: sakkizta yoʻnalish boʻyicha qayerda kuchlisiz va qayerda "
            "qoʻllab-quvvatlash kerak.\n\n"
            "Natija asosida platforma individual rejani taklif qiladi. Reja "
            "avtomatik ishga tushmaydi — uni siz koʻrib chiqasiz va oʻzingiz "
            "qabul qilasiz. Rozi boʻlmasangiz, u shunchaki taklif boʻlib qoladi.\n\n"
            "Bir necha oydan keyin diagnostikani takrorlash mumkin — shunda "
            "oʻzgarishni raqamda koʻrasiz.",
            "Развитие-балл — это не экзамен. Это снимок вашего сегодняшнего "
            "состояния: по восьми направлениям видно, где вы сильны, а где "
            "нужна поддержка.\n\n"
            "По результату платформа предложит индивидуальный план. План не "
            "запускается автоматически — вы его смотрите и принимаете сами. "
            "Если не согласны, он останется просто предложением.\n\n"
            "Через несколько месяцев диагностику можно пройти снова — и увидеть "
            "изменение в цифрах.",
            "The Development Score is not an exam. It is a picture of where you "
            "are today: across eight dimensions, where you are strong and where "
            "support would help.\n\n"
            "From the result the platform proposes an individual plan. The plan "
            "does not start by itself — you read it and accept it yourself. If "
            "you disagree, it stays a proposal and nothing more.\n\n"
            "In a few months you can take the assessment again and see the "
            "change as a number.",
        ),
    },
    {
        "slug": "grant-va-dastur-arizalari-ochiq",
        "category": NewsCategory.ANNOUNCEMENT,
        "tone": "sky",
        "emblem": "🗂",
        "tags": ["grant", "imkoniyat", "tadbirkorlik"],
        "source": "WomanUP · Invest HUB",
        "url": None,
        "minutes": 2,
        "adult": False,
        "pinned": False,
        "days_ago": 3,
        "title": (
            "Tadbirkor ayollar uchun grant arizalari ochiq",
            "Открыт приём заявок на гранты для женщин-предпринимателей",
            "Grant applications for women entrepreneurs are open",
        ),
        "summary": (
            "Invest HUB bilan integratsiya orqali arizani portalning oʻzidan yuborish mumkin.",
            "Через интеграцию с Invest HUB заявку можно отправить прямо с портала.",
            "Through the Invest HUB integration you can apply straight from the portal.",
        ),
        "body": (
            "«Imkoniyatlar» boʻlimida grantlar, vakansiyalar va investitsiya "
            "takliflari toʻplangan. Ular hamkor platformalardan keladi va "
            "profilingizga mos ravishda saralanadi.\n\n"
            "Ariza yuborishdan oldin portal sizdan aniq roziligingizni soʻraydi "
            "va nima uzatilishini koʻrsatadi: taxallus identifikatori va kasbiy "
            "maʼlumotlar. Telefon raqamingiz, pochtangiz va toʻliq ismingiz "
            "hamkorga yuborilmaydi.\n\n"
            "Roziligingizni istalgan vaqtda qaytarib olishingiz mumkin.",
            "В разделе «Возможности» собраны гранты, вакансии и инвестиционные "
            "предложения. Они приходят с партнёрских платформ и подбираются под "
            "ваш профиль.\n\n"
            "Перед отправкой заявки портал запрашивает ваше явное согласие и "
            "показывает, что именно будет передано: псевдонимный идентификатор "
            "и профессиональные данные. Телефон, почта и полное имя партнёру не "
            "уходят.\n\n"
            "Согласие можно отозвать в любой момент.",
            "The Opportunities section gathers grants, vacancies and investment "
            "offers. They arrive from partner platforms and are matched to your "
            "profile.\n\n"
            "Before an application is sent the portal asks for your explicit "
            "consent and shows exactly what will be shared: a pseudonymous id "
            "and professional details. Your phone number, e-mail and full name "
            "never reach the partner.\n\n"
            "You can withdraw that consent at any time.",
        ),
    },
    # ---- Women in science ---------------------------------------------
    {
        "slug": "katalin-kariko-mrna-nobel",
        "category": NewsCategory.SCIENCE,
        "tone": "ink",
        "emblem": "🔬",
        "tags": ["nobel", "mrna", "immunologiya"],
        "source": "Nobel Assembly, Karolinska Institutet (2023)",
        "url": "https://www.nobelprize.org/prizes/medicine/2023/summary/",
        "minutes": 3,
        "adult": False,
        "pinned": False,
        "days_ago": 2,
        "title": (
            "Katalin Karikó: oʻn yillik rad javoblardan Nobel mukofotigacha",
            "Каталин Карико: от десятилетий отказов до Нобелевской премии",
            "Katalin Karikó: from decades of rejection to a Nobel Prize",
        ),
        "summary": (
            "2023-yilgi fiziologiya va tibbiyot boʻyicha Nobel mukofoti mRNK vaksinalarini mumkin "
            "qilgan kashfiyot uchun berildi.",
            "Нобелевская премия 2023 года по физиологии и медицине — за открытие, которое сделало "
            "возможными мРНК-вакцины.",
            "The 2023 Nobel Prize in Physiology or Medicine went to the discovery that made mRNA "
            "vaccines possible.",
        ),
        "body": (
            "Katalin Karikó Vengriyada tugʻilgan biokimyogar. Uning gʻoyasi — "
            "hujayraga kerakli oqsilni oʻzi ishlab chiqarishni «aytib» beruvchi "
            "mRNK molekulasidan dori sifatida foydalanish — koʻp yillar davomida "
            "grant komissiyalari tomonidan rad etilgan. 1995-yilda universitetda "
            "uning lavozimi pasaytirilgan.\n\n"
            "Drew Weissman bilan birgalikda u asosiy toʻsiqni yechdi: sunʼiy "
            "mRNK organizmda kuchli yalligʻlanish reaksiyasini keltirib "
            "chiqarardi. Ular nukleozidlarni modifikatsiya qilish — molekulaning "
            "bir «harfini» almashtirish — bu reaksiyani susaytirishini "
            "koʻrsatishdi.\n\n"
            "Oʻsha modifikatsiya keyinchalik COVID-19ga qarshi mRNK "
            "vaksinalarining asosi boʻldi. 2023-yilda Karolinska instituti Nobel "
            "assambleyasi ikkalasiga fiziologiya va tibbiyot boʻyicha Nobel "
            "mukofotini berdi.\n\n"
            "Bu tarixning eng qiziq tomoni — kashfiyot toʻxtab qolmagani emas, "
            "balki uni davom ettirgan odam butun karerasi davomida «bu "
            "istiqbolsiz» degan javobni eshitgani.",
            "Каталин Карико — биохимик, родившаяся в Венгрии. Её идея "
            "использовать молекулу мРНК как лекарство — «сказать» клетке, какой "
            "белок произвести, — годами отклонялась грантовыми комиссиями. В "
            "1995 году её понизили в должности в университете.\n\n"
            "Вместе с Дрю Вайсманом она сняла главное препятствие: "
            "искусственная мРНК вызывала в организме сильную воспалительную "
            "реакцию. Они показали, что модификация нуклеозидов — замена одной "
            "«буквы» молекулы — эту реакцию гасит.\n\n"
            "Именно эта модификация легла в основу мРНК-вакцин против COVID-19. "
            "В 2023 году Нобелевская ассамблея Каролинского института присудила "
            "обоим премию по физиологии и медицине.\n\n"
            "Самое примечательное в этой истории не то, что открытие "
            "состоялось, а то, что человек, доведший его до конца, всю карьеру "
            "слышал: «это бесперспективно».",
            "Katalin Karikó is a Hungarian-born biochemist. Her idea — using an "
            "mRNA molecule as a medicine, telling a cell which protein to make "
            "— was turned down by grant committees for years. In 1995 she was "
            "demoted at her university.\n\n"
            "With Drew Weissman she removed the central obstacle: synthetic "
            "mRNA provoked a strong inflammatory response in the body. They "
            "showed that modifying its nucleosides — swapping a single 'letter' "
            "of the molecule — quiets that response.\n\n"
            "That modification became the foundation of the mRNA vaccines "
            "against COVID-19. In 2023 the Nobel Assembly at Karolinska "
            "Institutet awarded them both the prize in Physiology or Medicine.\n\n"
            "The striking part of the story is not that the discovery happened, "
            "but that the person who saw it through spent a career being told "
            "it was going nowhere.",
        ),
    },
    {
        "slug": "crispr-genom-tahriri-nobel",
        "category": NewsCategory.SCIENCE,
        "tone": "ink",
        "emblem": "🧬",
        "tags": ["nobel", "genetika", "crispr"],
        "source": "The Royal Swedish Academy of Sciences (2020)",
        "url": "https://www.nobelprize.org/prizes/chemistry/2020/summary/",
        "minutes": 3,
        "adult": False,
        "pinned": False,
        "days_ago": 5,
        "title": (
            "Genom «qaychisi»: kimyo boʻyicha Nobel mukofotini ikki ayol oldi",
            "«Ножницы» для генома: Нобелевскую премию по химии получили две женщины",
            "Genetic scissors: two women won the Nobel Prize in Chemistry",
        ),
        "summary": (
            "Emmanuelle Charpentier va Jennifer Doudna CRISPR-Cas9 usulini ishlab chiqdi — DNKni "
            "aniq nuqtada tahrirlash imkonini beradi.",
            "Эмманюэль Шарпантье и Дженнифер Даудна разработали метод CRISPR-Cas9 — он позволяет "
            "редактировать ДНК в точной позиции.",
            "Emmanuelle Charpentier and Jennifer Doudna developed CRISPR-Cas9, which edits DNA at "
            "a precise position.",
        ),
        "body": (
            "2020-yilda Shvetsiya Qirollik fanlar akademiyasi kimyo boʻyicha "
            "Nobel mukofotini Emmanuelle Charpentier va Jennifer Doudnaga berdi. "
            "Bu — mukofot tarixida birinchi marta kimyo boʻyicha ikki ayolga "
            "birgalikda berilgani.\n\n"
            "CRISPR-Cas9 dastlab bakteriyalarning viruslardan himoyalanish "
            "tizimi sifatida topilgan. Tadqiqotchilar uni universal vositaga "
            "aylantirishdi: endi DNK zanjirini oldindan belgilangan joyda kesish "
            "mumkin.\n\n"
            "Usul oʻroqsimon hujayrali anemiya va beta-talassemiya kabi irsiy "
            "kasalliklarni davolash usullarini ishlab chiqishda qoʻllanilmoqda, "
            "shuningdek qishloq xoʻjaligi va fundamental biologiyada.\n\n"
            "Charpentier mukofotdan keyin aytgan gap koʻp iqtibos keltiriladi: u "
            "yosh qizlarga fan «ayollar uchun ham» ekanini emas, balki fan "
            "ularniki ekanini koʻrsatishga umid qiladi.",
            "В 2020 году Шведская королевская академия наук присудила "
            "Нобелевскую премию по химии Эмманюэль Шарпантье и Дженнифер Даудне. "
            "Это первый случай в истории премии, когда химическую награду "
            "разделили две женщины.\n\n"
            "CRISPR-Cas9 изначально нашли как систему защиты бактерий от "
            "вирусов. Исследовательницы превратили её в универсальный "
            "инструмент: теперь цепочку ДНК можно разрезать в заранее заданном "
            "месте.\n\n"
            "Метод применяется при разработке методов лечения наследственных "
            "болезней — серповидноклеточной анемии и бета-талассемии, — а также "
            "в сельском хозяйстве и фундаментальной биологии.\n\n"
            "Фразу Шарпантье после награждения часто цитируют: она надеется "
            "показать девочкам не то, что наука «в том числе для женщин», а то, "
            "что наука — их.",
            "In 2020 the Royal Swedish Academy of Sciences awarded the Nobel "
            "Prize in Chemistry to Emmanuelle Charpentier and Jennifer Doudna — "
            "the first time in the prize's history that two women shared the "
            "chemistry award.\n\n"
            "CRISPR-Cas9 was first found as a bacterial defence system against "
            "viruses. The two researchers turned it into a general-purpose "
            "tool: a DNA strand can now be cut at a position chosen in advance.\n\n"
            "The method is being used to develop treatments for inherited "
            "diseases such as sickle-cell anaemia and beta-thalassaemia, as "
            "well as in agriculture and basic biology.\n\n"
            "Charpentier's line after the award is often quoted: she hopes to "
            "show girls not that science is 'also for women', but that science "
            "is theirs.",
        ),
    },
    {
        "slug": "andrea-ghez-qora-tuynuk",
        "category": NewsCategory.SCIENCE,
        "tone": "sky",
        "emblem": "🔭",
        "tags": ["nobel", "astronomiya", "fizika"],
        "source": "The Royal Swedish Academy of Sciences (2020)",
        "url": "https://www.nobelprize.org/prizes/physics/2020/summary/",
        "minutes": 2,
        "adult": False,
        "pinned": False,
        "days_ago": 8,
        "title": (
            "Andrea Ghez: galaktikamiz markazida nima borligini oʻlchagan ayol",
            "Андреа Гез: женщина, которая измерила, что находится в центре нашей галактики",
            "Andrea Ghez: the woman who measured what sits at our galaxy's centre",
        ),
        "summary": (
            "2020-yilgi fizika boʻyicha Nobel mukofoti Somon yoʻli markazidagi ulkan massali "
            "obyekt uchun berildi.",
            "Нобелевская премия по физике 2020 года — за сверхмассивный объект в центре Млечного "
            "Пути.",
            "The 2020 Nobel Prize in Physics recognised the supermassive object at the centre of "
            "the Milky Way.",
        ),
        "body": (
            "Andrea Ghez — fizika boʻyicha Nobel mukofotini olgan toʻrtinchi "
            "ayol. U oʻttiz yil davomida Somon yoʻli markazi atrofidagi "
            "yulduzlarning harakatini kuzatgan.\n\n"
            "Yulduzlar juda tez va juda kichik orbitada aylanardi. Buni faqat "
            "bitta narsa tushuntira olardi: markazda Quyoshdan millionlab marta "
            "ogʻirroq, lekin juda kichik hajmdagi obyekt bor.\n\n"
            "Kuzatish uchun u atmosfera buzilishini kompensatsiya qiluvchi "
            "adaptiv optikadan foydalandi — bu texnikaning oʻzi ham "
            "astronomiyani oʻzgartirdi.\n\n"
            "2020-yilda u Reinhard Genzel bilan mukofotni boʻlishdi — Yerdan "
            "koʻrinmaydigan narsani, uning atrofidagi harakat orqali oʻlchash "
            "mumkinligini isbotlagani uchun.",
            "Андреа Гез — четвёртая женщина, получившая Нобелевскую премию по "
            "физике. Тридцать лет она наблюдала за движением звёзд вокруг "
            "центра Млечного Пути.\n\n"
            "Звёзды двигались слишком быстро и по слишком тесным орбитам. "
            "Объяснение было только одно: в центре находится объект в миллионы "
            "раз тяжелее Солнца, но занимающий крошечный объём.\n\n"
            "Для наблюдений она применила адаптивную оптику, компенсирующую "
            "искажения атмосферы, — эта техника сама по себе изменила "
            "астрономию.\n\n"
            "В 2020 году она разделила премию с Райнхардом Генцелем — за "
            "доказательство того, что невидимое с Земли можно измерить по "
            "движению вокруг него.",
            "Andrea Ghez is the fourth woman to receive the Nobel Prize in "
            "Physics. For thirty years she tracked the motion of stars around "
            "the centre of the Milky Way.\n\n"
            "The stars moved too fast, on orbits too tight. Only one thing "
            "could explain it: an object at the centre millions of times "
            "heavier than the Sun, packed into a tiny volume.\n\n"
            "To see them she used adaptive optics, which cancels the blurring "
            "of the atmosphere — a technique that reshaped astronomy in its own "
            "right.\n\n"
            "In 2020 she shared the prize with Reinhard Genzel, for proving "
            "that something invisible from Earth can be measured by what moves "
            "around it.",
        ),
    },
    {
        "slug": "ayollar-ilm-fanda-har-uchinchi-tadqiqotchi",
        "category": NewsCategory.SCIENCE,
        "tone": "sand",
        "emblem": "📊",
        "tags": ["unesco", "ilm-fan", "statistika"],
        "source": "UNESCO Institute for Statistics",
        "url": "https://www.unesco.org/en/articles/women-science",
        "minutes": 2,
        "adult": False,
        "pinned": False,
        "days_ago": 11,
        "title": (
            "Dunyoda har uch tadqiqotchidan bittasi — ayol",
            "В мире женщина — каждый третий исследователь",
            "Worldwide, roughly one researcher in three is a woman",
        ),
        "summary": (
            "UNESCO maʼlumotlariga koʻra ayollar dunyo tadqiqotchilarining taxminan uchdan birini "
            "tashkil qiladi — muhandislik va IT sohalarida esa bundan ham kam.",
            "По данным ЮНЕСКО женщины составляют около трети исследователей мира — а в инженерии "
            "и IT их ещё меньше.",
            "UNESCO data puts women at about a third of the world's researchers — and fewer still "
            "in engineering and IT.",
        ),
        "body": (
            "UNESCO statistika instituti maʼlumotlariga koʻra, dunyodagi "
            "tadqiqotchilarning taxminan 33 foizi ayollar. Bu raqam oʻn yil "
            "ichida deyarli oʻzgarmadi.\n\n"
            "Farq soha boʻyicha keskin: hayot haqidagi fanlarda ayollar koʻp, "
            "muhandislik, informatika va sunʼiy intellektda esa ancha kam. "
            "Yuqori lavozimlarda va tadqiqot rahbarligida nomutanosiblik yanada "
            "kuchayadi.\n\n"
            "Sabab qobiliyatda emas. Tadqiqotlar koʻrsatishicha, asosiy yoʻqotish "
            "maktabdan universitetgacha va universitetdan ilmiy karerangacha "
            "boʻlgan oʻtishlarda sodir boʻladi — yaʼni tanlov qilinadigan "
            "nuqtalarda.\n\n"
            "Shuning uchun mentorlik va aniq namunalar muhim: qiz oʻz "
            "yoʻnalishida ayolni koʻrsa, bu yoʻl unga ham ochiq ekani "
            "koʻrinadi.",
            "По данным Института статистики ЮНЕСКО, женщины составляют около "
            "33% исследователей в мире. За десятилетие эта доля почти не "
            "изменилась.\n\n"
            "Разрыв резко различается по областям: в науках о жизни женщин "
            "много, в инженерии, информатике и искусственном интеллекте — "
            "заметно меньше. На руководящих позициях и в управлении "
            "исследованиями диспропорция усиливается.\n\n"
            "Причина не в способностях. Исследования показывают, что основные "
            "потери происходят на переходах: школа — университет и университет "
            "— научная карьера, то есть в точках выбора.\n\n"
            "Поэтому важны наставничество и конкретные примеры: если девушка "
            "видит женщину в своей области, этот путь становится для неё "
            "видимым.",
            "According to the UNESCO Institute for Statistics, women make up "
            "about 33% of the world's researchers. That share has barely moved "
            "in a decade.\n\n"
            "The gap varies sharply by field: women are well represented in the "
            "life sciences and much less so in engineering, computing and "
            "artificial intelligence. In senior posts and research leadership "
            "the imbalance widens further.\n\n"
            "The cause is not ability. Studies point to the transitions — "
            "school to university, university to a research career — as where "
            "the losses happen: the points where a choice is made.\n\n"
            "That is why mentorship and concrete examples matter: when a girl "
            "sees a woman working in her field, the path becomes visible to her "
            "too.",
        ),
    },
    # ---- Medicine ------------------------------------------------------
    {
        "slug": "bachadon-buyni-saratonini-oldini-olish",
        "category": NewsCategory.MEDICINE,
        "tone": "rose",
        "emblem": "🩺",
        "tags": ["profilaktika", "skrining", "hpv"],
        "source": "World Health Organization",
        "url": "https://www.who.int/initiatives/cervical-cancer-elimination-initiative",
        "minutes": 3,
        "adult": False,
        "pinned": False,
        "days_ago": 4,
        "title": (
            "Bachadon boʻyni saratoni — oldini olish mumkin boʻlgan kam sonli saratonlardan biri",
            "Рак шейки матки — один из немногих видов рака, который можно предотвратить",
            "Cervical cancer is one of the few cancers that can be prevented",
        ),
        "summary": (
            "JSST strategiyasi uchta raqamga asoslanadi: emlash, skrining va davolash qamrovi.",
            "Стратегия ВОЗ строится на трёх цифрах: охват вакцинацией, скринингом и лечением.",
            "The WHO strategy rests on three numbers: vaccination, screening and treatment "
            "coverage.",
        ),
        "body": (
            "Jahon sogʻliqni saqlash tashkiloti bachadon boʻyni saratonini yoʻq "
            "qilish boʻyicha global strategiyani qabul qilgan. U «90–70–90» "
            "deb ataladigan uchta maqsadga asoslanadi: 15 yoshgacha qizlarning "
            "90 foizini HPVga qarshi emlash, ayollarning 70 foizini yuqori "
            "aniqlikdagi test bilan skriningdan oʻtkazish va aniqlangan "
            "holatlarning 90 foizini davolash bilan taʼminlash.\n\n"
            "Kasallikning asosiy sababi — inson papillomavirusining yuqori "
            "onkogen turlari bilan uzoq davom etuvchi infeksiya. Aynan shuning "
            "uchun emlash profilaktika sifatida ishlaydi.\n\n"
            "Skrining muhim, chunki oʻzgarishlar saratonga aylanguncha yillar "
            "kerak boʻladi va bu bosqichda ular deyarli har doim davolanadi. "
            "Erta bosqichda kasallik odatda hech qanday belgi bermaydi — "
            "«hech narsa ogʻrimayapti» degani sogʻlom degani emas.\n\n"
            "Qaysi yoshda va qanday davriylikda tekshiruvdan oʻtish kerakligini "
            "shifokoringiz aytadi — bu yoshga va mahalliy protokolga bogʻliq.",
            "Всемирная организация здравоохранения приняла глобальную стратегию "
            "элиминации рака шейки матки. Она строится на трёх целях, известных "
            "как «90–70–90»: привить против ВПЧ 90% девочек до 15 лет, охватить "
            "скринингом высокоточным тестом 70% женщин и обеспечить лечением 90% "
            "выявленных случаев.\n\n"
            "Основная причина болезни — длительная инфекция онкогенными типами "
            "вируса папилломы человека. Именно поэтому вакцинация работает как "
            "профилактика.\n\n"
            "Скрининг важен потому, что изменениям нужны годы, чтобы стать "
            "раком, и на этой стадии они почти всегда излечимы. На ранней "
            "стадии болезнь обычно не даёт никаких симптомов — «ничего не "
            "болит» не означает «здорова».\n\n"
            "В каком возрасте и с какой периодичностью проходить обследование, "
            "скажет ваш врач — это зависит от возраста и местного протокола.",
            "The World Health Organization has adopted a global strategy to "
            "eliminate cervical cancer. It rests on three targets known as "
            "'90–70–90': vaccinate 90% of girls against HPV by age 15, screen "
            "70% of women with a high-performance test, and treat 90% of "
            "identified cases.\n\n"
            "The disease is caused by persistent infection with high-risk types "
            "of human papillomavirus. That is precisely why vaccination works "
            "as prevention.\n\n"
            "Screening matters because those changes take years to become "
            "cancer, and at that stage they are almost always treatable. Early "
            "on the disease usually causes no symptoms at all — 'nothing hurts' "
            "does not mean 'healthy'.\n\n"
            "Your doctor will tell you at what age and how often to be "
            "screened: it depends on your age and the local protocol.",
        ),
    },
    {
        "slug": "kokrak-saratonini-erta-aniqlash",
        "category": NewsCategory.MEDICINE,
        "tone": "rose",
        "emblem": "🎗",
        "tags": ["profilaktika", "skrining", "onkologiya"],
        "source": "World Health Organization · Global Breast Cancer Initiative",
        "url": "https://www.who.int/news-room/fact-sheets/detail/breast-cancer",
        "minutes": 3,
        "adult": False,
        "pinned": False,
        "days_ago": 6,
        "title": (
            "Koʻkrak saratoni: erta aniqlash omon qolish ehtimolini keskin oshiradi",
            "Рак груди: раннее выявление резко повышает шансы",
            "Breast cancer: early detection changes the odds dramatically",
        ),
        "summary": (
            "JSST maʼlumotlariga koʻra koʻkrak saratoni dunyoda ayollar orasida eng koʻp "
            "uchraydigan saraton turi.",
            "По данным ВОЗ, рак груди — самый распространённый вид рака среди женщин в мире.",
            "WHO data makes breast cancer the most common cancer among women worldwide.",
        ),
        "body": (
            "Koʻkrak saratoni — dunyoda ayollar orasida eng koʻp tashxis "
            "qoʻyiladigan saraton. JSSTning global tashabbusi oʻlim darajasini "
            "har yili 2,5 foizga kamaytirishni maqsad qilgan.\n\n"
            "Bu maqsadning asosi — murakkab texnologiya emas, balki uchta oddiy "
            "narsa: kasallik haqida bilim, tashxisning kechikmasligi va "
            "davolashning uzilishsiz boʻlishi.\n\n"
            "Amalda bu shuni anglatadi: koʻkrakdagi oʻzgarishni — tugun, shakl "
            "yoki teri oʻzgarishi, soʻrgʻichdan ajralma — sezgan ayol haftalar "
            "emas, kunlar ichida shifokorga murojaat qilishi kerak.\n\n"
            "Mammografiya skriningi qaysi yoshdan boshlanishi mamlakat "
            "protokoliga bogʻliq. Oilangizda koʻkrak yoki tuxumdon saratoni "
            "boʻlgan boʻlsa, buni albatta shifokorga ayting — bu tekshiruv "
            "jadvalini oʻzgartirishi mumkin.\n\n"
            "Bu post tashxis qoʻymaydi. Har qanday xavotirni koʻrikdan "
            "oʻtkazadigan yagona odam — shifokor.",
            "Рак груди — самый часто диагностируемый рак у женщин в мире. "
            "Глобальная инициатива ВОЗ ставит целью снижать смертность на 2,5% "
            "в год.\n\n"
            "В основе этой цели не сложная технология, а три простые вещи: "
            "знание о болезни, отсутствие задержки с диагнозом и непрерывность "
            "лечения.\n\n"
            "На практике это значит: женщина, заметившая изменение в груди — "
            "уплотнение, изменение формы или кожи, выделения из соска, — должна "
            "обратиться к врачу в течение дней, а не недель.\n\n"
            "С какого возраста начинается маммографический скрининг, зависит от "
            "протокола страны. Если в вашей семье был рак груди или яичников, "
            "обязательно скажите об этом врачу — это может изменить график "
            "обследований.\n\n"
            "Этот пост не ставит диагноз. Единственный, кто может оценить любую "
            "тревогу, — врач.",
            "Breast cancer is the most frequently diagnosed cancer in women "
            "worldwide. WHO's global initiative aims to reduce mortality by "
            "2.5% per year.\n\n"
            "What underpins that target is not advanced technology but three "
            "simple things: knowing about the disease, not delaying diagnosis, "
            "and treatment that is not interrupted.\n\n"
            "In practice this means a woman who notices a change in her breast "
            "— a lump, a change in shape or skin, discharge from the nipple — "
            "should see a doctor within days, not weeks.\n\n"
            "The age at which mammography screening begins depends on the "
            "national protocol. If breast or ovarian cancer runs in your "
            "family, tell your doctor — it may change your screening schedule.\n\n"
            "This post does not diagnose. The only person who can assess a "
            "worry is a doctor.",
        ),
    },
    {
        "slug": "homiladorlikda-folat-kislotasi",
        "category": NewsCategory.MEDICINE,
        "tone": "rose",
        "emblem": "🤍",
        "tags": ["homiladorlik", "profilaktika"],
        "source": "World Health Organization",
        "url": "https://www.who.int/tools/elena/interventions/folate-periconceptional",
        "minutes": 2,
        "adult": True,
        "pinned": False,
        "days_ago": 9,
        "title": (
            "Folat kislotasi homiladorlikni rejalashtirishdan oldin boshlanadi",
            "Фолиевая кислота начинается до планирования беременности",
            "Folic acid starts before you plan a pregnancy",
        ),
        "summary": (
            "JSST homiladorlikdan oldin va uning birinchi haftalarida folat kislotasini qabul "
            "qilishni tavsiya qiladi.",
            "ВОЗ рекомендует приём фолиевой кислоты до беременности и в её первые недели.",
            "WHO recommends folic acid before conception and through the first weeks of pregnancy.",
        ),
        "body": (
            "Asab naychasi nuqsonlari homiladorlikning birinchi haftalarida — "
            "koʻpincha ayol hali homiladorligini bilmagan paytda — shakllanadi. "
            "Aynan shuning uchun JSST folat kislotasini homiladorlikdan oldin "
            "boshlashni tavsiya qiladi.\n\n"
            "Bu qoida oddiy koʻrinadi, lekin uning maʼnosi vaqtda: "
            "homiladorlikni tasdiqlagandan keyin boshlangan qabul koʻpincha "
            "kech boʻladi.\n\n"
            "Dozani shifokor belgilaydi — u sizning anamnezingizga va boshqa "
            "qabul qilayotgan preparatlaringizga bogʻliq. Bu post dozani "
            "tavsiya qilmaydi.",
            "Дефекты нервной трубки формируются в первые недели беременности — "
            "часто когда женщина ещё не знает, что беременна. Именно поэтому "
            "ВОЗ рекомендует начинать приём фолиевой кислоты до беременности.\n\n"
            "Правило выглядит простым, но его смысл — во времени: приём, "
            "начатый после подтверждения беременности, часто уже поздний.\n\n"
            "Дозу определяет врач — она зависит от вашего анамнеза и других "
            "принимаемых препаратов. Этот пост дозу не назначает.",
            "Neural tube defects form in the first weeks of pregnancy — often "
            "before a woman knows she is pregnant. That is exactly why WHO "
            "recommends starting folic acid before conception.\n\n"
            "The rule looks simple, but its meaning is about timing: supplements "
            "started after a pregnancy is confirmed are often already late.\n\n"
            "The dose is set by your doctor and depends on your history and any "
            "other medication you take. This post does not prescribe one.",
        ),
    },
    # ---- Health --------------------------------------------------------
    {
        "slug": "temir-tanqisligi-kamqonlik",
        "category": NewsCategory.HEALTH,
        "tone": "sage",
        "emblem": "🌿",
        "tags": ["kamqonlik", "ovqatlanish", "charchoq"],
        "source": "World Health Organization",
        "url": "https://www.who.int/news-room/fact-sheets/detail/anaemia",
        "minutes": 3,
        "adult": False,
        "pinned": False,
        "days_ago": 7,
        "title": (
            "Doimiy charchoq har doim ham «shunchaki charchoq» emas",
            "Постоянная усталость не всегда «просто усталость»",
            "Constant tiredness is not always 'just tiredness'",
        ),
        "summary": (
            "JSST maʼlumotlariga koʻra reproduktiv yoshdagi ayollarning taxminan uchdan biri "
            "kamqonlikdan aziyat chekadi.",
            "По данным ВОЗ, примерно треть женщин репродуктивного возраста страдает анемией.",
            "WHO data suggests roughly a third of women of reproductive age have anaemia.",
        ),
        "body": (
            "Kamqonlik — dunyodagi eng keng tarqalgan ovqatlanish bilan bogʻliq "
            "muammolardan biri, va u ayollarga erkaklarga qaraganda ancha koʻp "
            "taʼsir qiladi. JSST baholariga koʻra, 15–49 yoshdagi ayollarning "
            "taxminan uchdan biri kamqonlikka ega.\n\n"
            "Belgilari koʻpincha kundalik hayotga «yozib qoʻyiladi»: charchoq, "
            "bosh aylanishi, teri oqarishi, diqqatni jamlashning qiyinlashuvi, "
            "sochning toʻkilishi. Ayol buni ish, uy yumushi yoki uyqusizlik "
            "bilan izohlaydi va yillar davomida shifokorga murojaat qilmaydi.\n\n"
            "Kamqonlikning oʻzi tashxis emas — bu belgi. Sababi temir "
            "tanqisligi ham, boshqa holat ham boʻlishi mumkin. Shuning uchun "
            "temir preparatlarini oʻz-oʻzidan boshlash notoʻgʻri: ortiqcha temir "
            "zararli, va asosiy sabab yashirinib qoladi.\n\n"
            "Toʻgʻri qadam oddiy — umumiy qon tahlili. Bu arzon, tez va "
            "koʻpincha savolga javob beradi.",
            "Анемия — одна из самых распространённых в мире проблем, связанных "
            "с питанием, и женщин она затрагивает заметно чаще мужчин. По "
            "оценкам ВОЗ, около трети женщин 15–49 лет живут с анемией.\n\n"
            "Симптомы обычно «списывают» на повседневность: усталость, "
            "головокружение, бледность, трудности с концентрацией, выпадение "
            "волос. Женщина объясняет это работой, домом или недосыпом и годами "
            "не доходит до врача.\n\n"
            "Сама анемия — не диагноз, а признак. Причиной может быть дефицит "
            "железа, а может — другое состояние. Поэтому начинать приём железа "
            "самостоятельно неправильно: избыток железа вреден, а настоящая "
            "причина остаётся скрытой.\n\n"
            "Правильный шаг простой — общий анализ крови. Он недорогой, быстрый "
            "и часто отвечает на вопрос.",
            "Anaemia is one of the most widespread nutrition-related problems "
            "in the world, and it affects women considerably more than men. WHO "
            "estimates that around a third of women aged 15–49 live with it.\n\n"
            "The symptoms are usually written off as ordinary life: tiredness, "
            "dizziness, pallor, trouble concentrating, hair loss. A woman puts "
            "it down to work, the house or poor sleep, and goes years without "
            "seeing a doctor.\n\n"
            "Anaemia itself is not a diagnosis — it is a sign. The cause may be "
            "iron deficiency, or something else entirely. Starting iron on your "
            "own is therefore the wrong move: excess iron is harmful, and the "
            "real cause stays hidden.\n\n"
            "The right step is a simple one — a full blood count. It is cheap, "
            "quick, and often answers the question.",
        ),
    },
    {
        "slug": "haftasiga-150-daqiqa-harakat",
        "category": NewsCategory.HEALTH,
        "tone": "sage",
        "emblem": "🚶‍♀️",
        "tags": ["harakat", "odat", "profilaktika"],
        "source": "World Health Organization",
        "url": "https://www.who.int/news-room/fact-sheets/detail/physical-activity",
        "minutes": 2,
        "adult": False,
        "pinned": False,
        "days_ago": 10,
        "title": (
            "Haftasiga 150 daqiqa — sport zali shart emas",
            "150 минут в неделю — спортзал не обязателен",
            "150 minutes a week — no gym required",
        ),
        "summary": (
            "JSST kattalar uchun haftasiga 150–300 daqiqa oʻrtacha jadallikdagi harakatni tavsiya "
            "qiladi.",
            "ВОЗ рекомендует взрослым 150–300 минут умеренной активности в неделю.",
            "WHO recommends 150–300 minutes of moderate activity a week for adults.",
        ),
        "body": (
            "JSSTning jismoniy faollik boʻyicha tavsiyasi kattalar uchun bitta "
            "raqamga sigʻadi: haftasiga 150 dan 300 daqiqagacha oʻrtacha "
            "jadallikdagi harakat. Bu kuniga taxminan 20–40 daqiqa.\n\n"
            "«Oʻrtacha jadallik» — bu yugurish shart emas degani. Tez yurish, "
            "zinapoya, velosiped, uy yumushlari va raqs ham hisobga oladi. "
            "Meʼyor — gapira olasiz, lekin qoʻshiq ayta olmaysiz.\n\n"
            "Bu tavsiyaning eng foydali qismi — uni boʻlaklarga boʻlish mumkin. "
            "Kuniga uch marta oʻn daqiqa — bu bir marta oʻttiz daqiqa bilan "
            "bir xil hisoblanadi.\n\n"
            "Har qanday harakat harakatsizlikdan yaxshiroq. Agar hozir nolga "
            "yaqin boʻlsangiz, birinchi maqsad 150 daqiqa emas — birinchi "
            "maqsad bugungi yurish.",
            "Рекомендация ВОЗ по физической активности умещается для взрослых в "
            "одну цифру: от 150 до 300 минут умеренной активности в неделю. Это "
            "примерно 20–40 минут в день.\n\n"
            "«Умеренная активность» означает, что бегать не обязательно. "
            "Быстрая ходьба, лестница, велосипед, домашние дела и танцы тоже "
            "считаются. Ориентир — вы можете говорить, но не можете петь.\n\n"
            "Самая полезная часть этой рекомендации в том, что её можно дробить. "
            "Три раза по десять минут в день считаются так же, как один "
            "тридцатиминутный подход.\n\n"
            "Любое движение лучше его отсутствия. Если сейчас у вас почти ноль, "
            "первая цель — не 150 минут, а сегодняшняя прогулка.",
            "WHO's physical activity guidance fits into a single number for "
            "adults: 150 to 300 minutes of moderate activity per week. That is "
            "roughly 20–40 minutes a day.\n\n"
            "'Moderate' means running is not required. Brisk walking, stairs, "
            "cycling, housework and dancing all count. The rule of thumb: you "
            "can talk, but you cannot sing.\n\n"
            "The most useful part of the guidance is that it can be broken up. "
            "Three ten-minute bouts in a day count the same as one block of "
            "thirty.\n\n"
            "Any movement beats none. If you are near zero right now, the first "
            "target is not 150 minutes — it is today's walk.",
        ),
    },
    {
        "slug": "ruhiy-salomatlik-va-charchoq",
        "category": NewsCategory.HEALTH,
        "tone": "sky",
        "emblem": "🫧",
        "tags": ["ruhiy salomatlik", "charchoq", "uyqu"],
        "source": "World Health Organization",
        "url": "https://www.who.int/news-room/fact-sheets/detail/mental-health-strengthening-our-response",
        "minutes": 3,
        "adult": False,
        "pinned": False,
        "days_ago": 13,
        "title": (
            "Charchoq — dangasalik emas, signal",
            "Выгорание — не лень, а сигнал",
            "Burnout is not laziness — it is a signal",
        ),
        "summary": (
            "JSST charchoqni kasallik emas, ish bilan bogʻliq va bartaraf etilmagan surunkali "
            "stress hodisasi sifatida taʼriflaydi.",
            "ВОЗ описывает выгорание не как болезнь, а как явление хронического рабочего стресса, "
            "с которым не справились.",
            "WHO describes burnout not as a disease but as a phenomenon of chronic workplace "
            "stress that was not managed.",
        ),
        "body": (
            "Xalqaro kasalliklar tasnifida charchoq (burnout) kasallik emas, "
            "balki «kasbiy hodisa» sifatida taʼriflangan: bartaraf etilmagan "
            "surunkali ish stressi natijasi. Uning uchta belgisi bor — "
            "quvvatning tugashi, ishdan ruhiy uzoqlashish va samaradorlikning "
            "pasayishi.\n\n"
            "Ayollar uchun bu koʻpincha ikki qavatli boʻladi: ish va uydagi "
            "koʻrinmas mehnat. Ikkinchisi jadvalga yozilmaydi, shuning uchun "
            "«men shunchaki dam olmayapman» degan tuygʻu tushunarsiz "
            "koʻrinadi.\n\n"
            "Nima yordam beradi: uyqu rejimini tiklash, kunda ish bilan "
            "bogʻliq boʻlmagan aniq vaqt, yordam soʻrash koʻnikmasi va "
            "yuklamani baham koʻrish. Bular kichik qadamlar, lekin ular "
            "tekshirilgan.\n\n"
            "Agar holat ikki haftadan koʻproq davom etsa, kundalik ishlarga "
            "xalaqit bersa yoki oʻzingizga zarar yetkazish fikrlari paydo "
            "boʻlsa — bu mutaxassisga murojaat qilish sababi, kuchsizlik "
            "belgisi emas.",
            "В международной классификации болезней выгорание описано не как "
            "болезнь, а как «профессиональное явление»: результат хронического "
            "рабочего стресса, с которым не справились. У него три признака — "
            "истощение, психологическое отдаление от работы и снижение "
            "результативности.\n\n"
            "У женщин это часто двухслойно: работа плюс невидимый труд дома. "
            "Второй в расписание не записывается, поэтому ощущение «я просто не "
            "отдыхаю» выглядит необъяснимым.\n\n"
            "Что помогает: восстановление режима сна, чёткое время в дне, не "
            "связанное с работой, навык просить помощь и разделение нагрузки. "
            "Это небольшие шаги, но проверенные.\n\n"
            "Если состояние длится дольше двух недель, мешает обычным делам или "
            "появляются мысли о причинении себе вреда — это повод обратиться к "
            "специалисту, а не признак слабости.",
            "In the international classification of diseases, burnout is "
            "described not as a disease but as an 'occupational phenomenon': "
            "the result of chronic workplace stress that was not successfully "
            "managed. It has three markers — exhaustion, mental distance from "
            "the job, and reduced effectiveness.\n\n"
            "For women it is often two-layered: paid work plus invisible work "
            "at home. The second never goes on a schedule, which is why the "
            "feeling of 'I just never rest' seems inexplicable.\n\n"
            "What helps: restoring a sleep pattern, a defined part of the day "
            "with no work in it, the skill of asking for help, and sharing the "
            "load. These are small steps, but they are tested ones.\n\n"
            "If it lasts more than two weeks, interferes with ordinary tasks, "
            "or thoughts of harming yourself appear — that is a reason to see a "
            "professional, not a sign of weakness.",
        ),
    },
    # ---- Career and stories --------------------------------------------
    {
        "slug": "raqamli-koniklar-va-ish-bozori",
        "category": NewsCategory.CAREER,
        "tone": "sand",
        "emblem": "💼",
        "tags": ["kasb", "raqamli koʻnikma", "ish"],
        "source": "WomanUP · Edu-Job",
        "url": None,
        "minutes": 2,
        "adult": False,
        "pinned": False,
        "days_ago": 12,
        "title": (
            "Ish bozorida eng koʻp soʻralayotgan koʻnikmalar",
            "Какие навыки чаще всего спрашивают на рынке труда",
            "The skills employers ask for most",
        ),
        "summary": (
            "Portaldagi vakansiyalar tahlili: raqamli savodxonlik, hisobot va mijoz bilan muloqot.",
            "Анализ вакансий на портале: цифровая грамотность, отчётность и работа с клиентом.",
            "An analysis of vacancies on the portal: digital literacy, reporting and client work.",
        ),
        "body": (
            "«Imkoniyatlar» boʻlimidagi vakansiyalarni koʻrsangiz, bir naqsh "
            "koʻrinadi: talab qilinadigan koʻnikmalarning katta qismi — bu "
            "diplom emas, balki aniq amaliy mahoratlar.\n\n"
            "Eng koʻp uchraydiganlari: elektron jadvallar bilan ishlash, oddiy "
            "hisobot tayyorlash, mijoz bilan yozma muloqot, ijtimoiy tarmoqda "
            "kontent va buxgalteriya dasturlarining boshlangʻich darajasi.\n\n"
            "Bu yaxshi xabar, chunki bunday koʻnikmalar bir necha oyda "
            "oʻzlashtiriladi. Portaldagi dasturlar katalogi aynan shu "
            "koʻnikmalar boʻyicha qidiruvni qoʻllab-quvvatlaydi — kurs nomi "
            "bilan emas, siz qila olishni istagan ish bilan.\n\n"
            "Agar diagnostikadan oʻtgan boʻlsangiz, reja shu boʻshliqlarni "
            "hisobga oladi.",
            "Если просмотреть вакансии в разделе «Возможности», виден один "
            "рисунок: большая часть требуемых навыков — это не диплом, а "
            "конкретные прикладные умения.\n\n"
            "Чаще всего встречаются: работа с электронными таблицами, "
            "подготовка простого отчёта, письменная коммуникация с клиентом, "
            "контент в соцсетях и начальный уровень бухгалтерских программ.\n\n"
            "Это хорошая новость, потому что такие навыки осваиваются за "
            "несколько месяцев. Каталог программ на портале поддерживает поиск "
            "именно по навыкам — не по названию курса, а по тому, что вы хотите "
            "уметь делать.\n\n"
            "Если вы прошли диагностику, план учитывает эти пробелы.",
            "Look through the vacancies in the Opportunities section and one "
            "pattern stands out: most of the skills asked for are not a degree "
            "but specific, practical abilities.\n\n"
            "The most frequent are spreadsheets, putting together a simple "
            "report, written communication with a client, social media content, "
            "and a basic level of accounting software.\n\n"
            "That is good news, because skills like these take months, not "
            "years. The programme catalogue on the portal supports searching by "
            "skill — not by course title, but by what you want to be able to "
            "do.\n\n"
            "If you have taken the assessment, your plan already accounts for "
            "those gaps.",
        ),
    },
    {
        "slug": "mentorlik-sessiyalari-ochiq",
        "category": NewsCategory.SUCCESS_STORY,
        "tone": "plum",
        "emblem": "✨",
        "tags": ["mentorlik", "hikoya"],
        "source": "WomanUP",
        "url": None,
        "minutes": 2,
        "adult": False,
        "pinned": False,
        "days_ago": 14,
        "title": (
            "Mentorlik: birinchi suhbat odatda eng qiyini",
            "Наставничество: первый разговор обычно самый трудный",
            "Mentorship: the first conversation is usually the hardest",
        ),
        "summary": (
            "Mentorlar katalogi ochiq — sessiyani portaldan bron qilish mumkin.",
            "Каталог наставниц открыт — сессию можно забронировать на портале.",
            "The mentor directory is open — you can book a session from the portal.",
        ),
        "body": (
            "Mentorlik — bu bepul maslahat emas. Bu sizning oʻrningizda "
            "boʻlgan odam bilan suhbat: u qanday tanlov qilganini va nimadan "
            "afsuslanganini bilasiz.\n\n"
            "Amaliyot koʻrsatadiki, eng qiyin qadam — birinchi xabar. Shuning "
            "uchun portal sessiyani soʻrashni qisqa shaklga aylantirdi: "
            "mavzuni tanlaysiz, savolingizni yozasiz va vaqt taklif qilasiz.\n\n"
            "Birinchi suhbatga tayyorgarlik uchun uchta savol yetarli: hozir "
            "nima toʻsib turibdi, oʻn ikki oydan keyin qayerda boʻlishni "
            "istaysiz va qaysi qadamni yolgʻiz qila olmayapsiz.",
            "Наставничество — это не бесплатная консультация. Это разговор с "
            "человеком, который был на вашем месте: вы узнаёте, какой выбор он "
            "сделал и о чём жалеет.\n\n"
            "Практика показывает, что самый трудный шаг — первое сообщение. "
            "Поэтому портал свёл запрос сессии к короткой форме: выбираете "
            "тему, пишете свой вопрос и предлагаете время.\n\n"
            "Для подготовки к первому разговору достаточно трёх вопросов: что "
            "мешает сейчас, где вы хотите быть через двенадцать месяцев и какой "
            "шаг вы не можете сделать в одиночку.",
            "Mentorship is not free consulting. It is a conversation with "
            "someone who has stood where you are: you learn what choice they "
            "made and what they regret.\n\n"
            "In practice the hardest step is the first message. So the portal "
            "reduced requesting a session to a short form: pick a topic, write "
            "your question, propose a time.\n\n"
            "Three questions are enough to prepare for a first conversation: "
            "what is blocking you now, where you want to be in twelve months, "
            "and which step you cannot take alone.",
        ),
    },
]


async def load_news(session: AsyncSession, now: datetime | None = None) -> int:
    """Write the feed, matching on `slug`.

    Non-destructive on purpose, and separate from `app.seed` for a reason worth
    keeping: the demo seeder clears the tables it owns, and `users` is one of
    them — running it to refresh the feed also deletes every account anyone has
    registered since. Loading the feed must never cost somebody her profile.

    Re-runnable: an existing post is updated in place, so its id and any link
    already shared to it survive.
    """
    now = now or datetime.now(UTC)
    written = 0

    for item in POSTS:
        post = await session.scalar(select(NewsPost).where(NewsPost.slug == item["slug"]))
        if post is None:
            post = NewsPost(slug=item["slug"])
            session.add(post)

        post.category = item["category"]
        post.title_i18n = _tri(item["title"])
        post.summary_i18n = _tri(item["summary"])
        post.body_i18n = _tri(item["body"])
        post.cover_url = item.get("photo")
        post.cover_tone = item["tone"]
        post.cover_emblem = item["emblem"]
        post.source_name = item["source"]
        post.source_url = item["url"]
        post.tags = item["tags"]
        post.reading_minutes = item["minutes"]
        post.is_adult_only = item["adult"]
        post.is_pinned = item["pinned"]
        post.is_published = True
        # Counted back from now, so the feed reads as current whenever it is
        # loaded rather than dated to the day this file was written.
        post.published_at = now - timedelta(days=item["days_ago"])

        # Scored from the text that was just written onto the post, so the
        # ordering of these two statements matters.
        apply_analysis(post, rule_based(post))
        written += 1

    await session.flush()
    return written


async def _main() -> None:
    from app.core.logging import configure_logging
    from app.db import SessionLocal

    configure_logging()
    async with SessionLocal() as session:
        count = await load_news(session)
        await session.commit()
    logger.info("loaded %s news posts", count)
    print(f"\n  {count} ta yangilik yuklandi. Boshqa hech narsa oʻzgartirilmadi.\n")


if __name__ == "__main__":
    asyncio.run(_main())
