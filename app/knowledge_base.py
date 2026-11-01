"""The portal's own documentation: what the AI assistant may say about WomanUP.

Fourteen short documents, each in Uzbek with Russian and English editions,
approved for retrieval. They are the only source the assistant quotes when a
woman asks about the portal itself — how to register, what the Development
Score is, how a course or an application works, what happens to her data. A
question the documents do not cover gets "I do not know", never a guess.

Kept as data, in one place, for two readers: the demonstration seed, and the
migration that loads them into a production database the seed must never
touch. Edit the text here; both pick it up. Keep every sentence true of the
product as it is — the assistant repeats what is written.
"""

from __future__ import annotations

KNOWLEDGE_TRANSLATIONS: dict[str, dict[str, tuple[str, str]]] = {
    "WomanUP portali nima va u qanday ishlaydi": {
        "ru": (
            "Что такое портал WomanUP и как он работает",
            """
WomanUP — национальная цифровая платформа развития для женщин и девушек. Портал
регистрирует вас, определяет ваши потребности, составляет индивидуальный план
развития и связывает с реальными возможностями.

Путь на портале состоит из пяти шагов. Сначала вы регистрируетесь по электронной
почте и паролю (или через аккаунт Google) и получаете WomanUP ID. Затем проходите диагностику на 10-15 минут.
После этого система показывает ваш Development Score и предлагает индивидуальный
план. Дальше вы записываетесь на программы и начинаете учиться. И наконец, вам
рекомендуют подходящие возможности — вакансию, грант или торговую площадку.

Пользование порталом бесплатное. Основные услуги открыты всем пользователям.
""",
        ),
        "en": (
            "What the WomanUP portal is and how it works",
            """
WomanUP is a national digital development platform for women and girls. The
portal registers you, works out what you need, builds an individual development
plan and connects you to real opportunities.

The journey has five steps. First you register with your e-mail address and a
password (or with a Google account) and receive a WomanUP ID. Second, you take a 10-15 minute diagnostic. Third, the
system shows your Development Score and proposes an individual plan. Fourth, you
enrol on programmes and start studying. Fifth, matching opportunities — a
vacancy, a grant or a marketplace slot — are recommended to you.

Using the portal is free. The core services are open to every user.
""",
        ),
    },
    "Development Score qanday hisoblanadi": {
        "ru": (
            "Как рассчитывается Development Score",
            """
WomanUP Development Score — это составной показатель от 0 до 100. Он считается по
восьми измерениям: образование и навыки, карьера и занятость, предпринимательство,
финансовая грамотность, цифровые навыки, здоровье и баланс, семья и отношения,
лидерство и глобальные возможности. Диагностика состоит из 25 вопросов и занимает
10-15 минут.

Каждое измерение оценивается отдельно, затем значения сводятся во взвешенное
среднее. Первый результат сохраняется как базовый, и все последующие
сравниваются именно с ним.

Балл — не оценка и не приговор. Он нужен, чтобы найти направление, где поддержка
нужнее всего, и построить план именно от него. Низкий балл в каком-то измерении
означает лишь, что там больше пространства для роста.
""",
        ),
        "en": (
            "How the Development Score is calculated",
            """
The WomanUP Development Score is a composite figure from 0 to 100. It is
calculated across eight dimensions: education and skills, career and employment,
entrepreneurship, financial literacy, digital skills, health and balance, family and
relationships, leadership and global opportunities. The diagnostic has 25 questions
and takes 10-15 minutes.

Each dimension is scored separately, then combined into a weighted average. Your
first result is stored as the baseline, and every later result is compared
against it.

The score is not a grade or a verdict. It exists to find where support is most
needed and to build your plan from there. A low score in one dimension only
means there is more room to grow in it.
""",
        ),
    },
    "Shaxsiy maʼlumotlaringiz qanday himoyalanadi": {
        "ru": (
            "Как защищены ваши персональные данные",
            """
Ваши данные хранятся на портале и не передаются третьим лицам без вашего
согласия. Каждое согласие фиксируется отдельно и может быть отозвано в любой
момент.

Партнёрам передаётся минимальный набор: псевдонимный идентификатор, регион,
профессиональные навыки и уровень образования. Номер телефона, полное имя,
семейное положение и число детей не передаются никогда.

Чувствительные поля — здоровье, семья — имеют отдельную область доступа. Каждое
обращение к ним записывается в журнал аудита.
""",
        ),
        "en": (
            "How your personal data is protected",
            """
Your data is held on the portal and is not passed to third parties without your
consent. Each consent is recorded separately and can be withdrawn at any time.

Partners receive a minimal set: a pseudonymous identifier, your region, your
professional skills and your education level. Your phone number, full name,
marital status and number of children are never sent.

Sensitive fields — health, family — sit behind a separate access scope. Every
access to them is written to the audit log.
""",
        ),
    },
    "Kursga qanday yozilaman va sertifikat olamanmi": {
        "ru": (
            "Как записаться на курс и получу ли я сертификат",
            """
Выберите курс в каталоге программ и нажмите «Записаться». Курс появится в разделе
«Мои курсы» вашего личного кабинета, и вы сможете продолжить в любое время.

Курс состоит из модулей. Прогресс сохраняется автоматически. Когда прогресс
Когда программа пройдена на 100 процентов, сертификат выдаётся автоматически.

Сертификат предусмотрен не во всех курсах: это указано в карточке курса. Курсы
без сертификата всё равно учитываются в вашем плане развития.
""",
        ),
        "en": (
            "How to enrol on a course and whether you get a certificate",
            """
Pick a course from the programme catalogue and press "Enrol". The course appears
under "My courses" in your personal cabinet, and you can pick it up whenever you
like.

A course is made of modules. Progress saves automatically. When progress reaches
When a programme reaches 100 percent, the certificate is issued automatically.

Not every course carries a certificate; the course card says so explicitly.
Courses without one still count towards your development plan.
""",
        ),
    },
    "Ishga joylashish uchun nima qilishim kerak": {
        "ru": (
            "Что нужно сделать, чтобы найти работу",
            """
Сначала заполните профиль: образование, профессия, навыки и регион. Чем полнее
профиль, тем точнее подбор вакансий.

Затем дайте согласие на передачу данных в Edu-Job. Без согласия вакансии
показываются, но подать заявку нельзя — это защитная мера.

Если навыков не хватает, система покажет, каких именно, и предложит курс,
который их закрывает. После курса ваш профиль обновляется, и подбор становится
точнее.

Отклик на вакансию отслеживается в кабинете: отправлена, на рассмотрении,
принята или отклонена.
""",
        ),
        "en": (
            "What to do to find work",
            """
Start by completing your profile: education, profession, skills and region. The
fuller the profile, the better the vacancy matching.

Then give consent to share your data with Edu-Job. Without it you can still see
vacancies but cannot apply — that is a safeguard, not an obstacle.

If you are short of a skill, the system names which one and suggests the course
that closes the gap. After the course your profile updates and the matching
improves.

Every application is tracked in your cabinet: submitted, under review, accepted
or rejected.
""",
        ),
    },
    "Biznes ochish uchun qanday yordam bor": {
        "ru": (
            "Какая поддержка есть для открытия бизнеса",
            """
На портале три вида поддержки предпринимательства: обучение, менторство и
финансирование.

Обучение — курсы по основам предпринимательства, финансовой модели и продажам. С
них стоит начать, если бизнеса ещё нет.

Менторство — встречи с предпринимательницей, которая уже прошла этот путь.
Ментор не принимает решения за вас, а показывает дорогу.

Финансирование идёт через Invest HUB: безвозвратные гранты, льготные микрокредиты
и долевые инвестиции. Для любой заявки нужны бизнес-план и согласие на передачу
данных.
""",
        ),
        "en": (
            "What support there is for starting a business",
            """
The portal offers three kinds of support for entrepreneurship: training,
mentoring and funding.

Training covers the basics of business, the financial model and selling. Start
here if the business does not exist yet.

Mentoring means meeting a woman who has already walked this road. A mentor does
not decide for you; she shows you the way.

Funding runs through Invest HUB: non-repayable grants, subsidised microloans and
equity investment. Every application needs a business plan and your consent to
share data.
""",
        ),
    },
    "Dasturga qanday yozilish va sertifikat olish mumkin": {
        "ru": (
            "Как записаться на программу и получить сертификат",
            """
Чтобы записаться, выберите подходящую программу в каталоге и нажмите
«Записаться». После этого программа появится в разделе «Мои программы» вашего
кабинета, и вы сможете вернуться к ней в любой момент.

Каждая программа состоит из модулей. Когда вы завершаете модуль, прогресс
сохраняется автоматически. Когда программа пройдена на 100 процентов, сертификат выдаётся автоматически. Сертификат скачивается из кабинета.

Сертификат есть не у всех программ — это отдельно отмечено в карточке программы.
Программы без сертификата тоже учитываются в вашем плане.
""",
        ),
        "en": (
            "How to enrol on a programme and get a certificate",
            """
To enrol, choose a programme from the catalogue and press "Enrol". The programme
then appears under "My programmes" in your cabinet, and you can return to it
whenever you like.

Every programme is made of modules. Finishing a module saves your progress
automatically. When the programme reaches 100 per cent, the certificate is issued automatically. You download it from your cabinet.

Not every programme carries a certificate — the programme card states this
separately. Programmes without one still count towards your plan.
""",
        ),
    },
    "Individual rivojlanish rejasi qanday tuziladi": {
        "ru": (
            "Как составляется индивидуальный план развития",
            """
План строится по результатам диагностики. Система определяет измерения с самым
низким баллом и предлагает конкретные шаги именно по этим направлениям. Каждый
шаг может быть привязан к программе или возможности.

Важное правило: план не вступает в силу автоматически. Система только предлагает
— решение за вами. Вы просматриваете план и подтверждаете его кнопкой
«Принять». Неподтверждённый план не считается активным.

План можно пересоздать в любой момент — например, после новой диагностики или
когда изменилась цель. Старый план сохраняется в истории.
""",
        ),
        "en": (
            "How your individual development plan is built",
            """
The plan is built from your diagnostic results. The system identifies the
lowest-scoring dimensions and proposes concrete steps in exactly those
directions. Each step may be tied to a programme or an opportunity.

One rule matters most: the plan does not take effect on its own. The system only
proposes — the decision is yours. You review the plan and confirm it with the
"Accept" button. An unconfirmed plan is not treated as active.

You can regenerate the plan at any time — after a new diagnostic, say, or when
your goal changes. The old plan is kept in your history.
""",
        ),
    },
    "Mening maʼlumotlarim kimga va qachon uzatiladi": {
        "ru": (
            "Кому и когда передаются мои данные",
            """
Ваши данные никогда не покидают портал без вашего согласия. Для каждой
партнёрской системы — Edu-Job, Invest HUB, Тижорат Маркази — согласие
запрашивается отдельно.

Даже когда согласие дано, партнёру передаётся только минимум: псевдонимный
идентификатор, регион, профессиональные навыки и уровень образования. Номер
телефона, полное имя, семейное положение и число детей не передаются никогда.

Согласие можно отозвать в любой момент — в разделе «Согласия» вашего кабинета.
После отзыва новые передачи прекращаются.
""",
        ),
        "en": (
            "Who receives my data, and when",
            """
Your data never leaves the portal without your consent. Consent is asked
separately for each partner system — Edu-Job, Invest HUB, Tijorat Markazi.

Even once consent is given, the partner receives only the minimum: a pseudonymous
identifier, your region, your professional skills and your education level. Your
phone number, full name, marital status and number of children are never sent.

You can withdraw consent at any time, in the "Consents" section of your cabinet.
After withdrawal, no further transfers are made.
""",
        ),
    },
    "Imkoniyatga ariza berish tartibi": {
        "ru": (
            "Как подать заявку на возможность",
            """
В разделе возможностей вам рекомендуются подходящие вакансии, гранты и торговые
площадки. Соответствие считается по вашим навыкам, региону и плану развития.

Чтобы подать заявку, сначала нужно дать согласие на передачу данных
соответствующей партнёрской системе. Без согласия система заявку не примет — это
защитная мера.

Статус заявки виден в кабинете: отправлена, на рассмотрении, принята или
отклонена. Когда приходит ответ от партнёра, вам приходит уведомление.
""",
        ),
        "en": (
            "How to apply for an opportunity",
            """
The opportunities section recommends vacancies, grants and marketplace slots that
suit you. The match is calculated from your skills, your region and your
development plan.

To apply you must first consent to sharing your data with that partner system.
Without consent the system will not accept the application — that is a
safeguard.

The status is visible in your cabinet: submitted, under review, accepted or
rejected. When the partner responds, you receive a notification.
""",
        ),
    },
    "Mentor bilan qanday ishlash kerak": {
        "ru": (
            "Как работать с ментором",
            """
Ментор — это практик, который делится своим опытом. Ментор не принимает решения
за вас и не находит вам работу; он показывает путь.

Подготовьтесь к первой встрече: запишите, на какой вопрос хотите получить ответ
и какого результата ждёте. Встреча обычно длится 45-60 минут.

Менторство бесплатное и добровольное. Если не подошло — можно выбрать другого
ментора, это нормально.
""",
        ),
        "en": (
            "How to work with a mentor",
            """
A mentor is a practitioner who shares her own experience. A mentor does not make
decisions for you and does not find you a job; she shows you the way.

Prepare for the first meeting: write down the question you want answered and the
result you are hoping for. A session usually runs 45-60 minutes.

Mentoring is free and voluntary. If the fit is wrong you can choose a different
mentor — that is normal.
""",
        ),
    },
    "Mehnat huquqlari: dekret, taʼtil va shartnoma": {
        "ru": (
            "Трудовые права: декрет, отпуск и договор",
            """
Трудовой договор должен быть заключён в письменной форме. В нём указываются
должность, оплата, рабочее время и продолжительность отпуска. Устная
договорённость вас не защищает.

Ежегодный оплачиваемый отпуск составляет не менее 15 рабочих дней. Отпуск по
беременности и родам установлен законом отдельно, и отменить его нельзя.

Работодатель не вправе отказать в приёме на работу или расторгнуть договор по
причине беременности. Если ваши права нарушены, можно обратиться в трудовую
инспекцию.

Это общая информация. По конкретной ситуации проконсультируйтесь с юристом.
""",
        ),
        "en": (
            "Employment rights: maternity leave, holiday and contracts",
            """
An employment contract must be in writing. It states the role, the pay, the
working hours and the length of holiday. A verbal agreement does not protect
you.

Paid annual leave is at least 15 working days. Maternity leave is set separately
in law and cannot be taken away.

An employer may not refuse to hire you or terminate your contract because of
pregnancy. If your rights are breached, you can go to the labour inspectorate.

This is general information. For your specific situation, consult a lawyer.
""",
        ),
    },
    "Raqamli xavfsizlik: firibgarlikdan himoya": {
        "ru": (
            "Цифровая безопасность: защита от мошенничества",
            """
Никому не сообщайте код из SMS. Ни банк, ни портал, ни государственное
учреждение никогда не спрашивают код. Любой, кто его просит, — мошенник.

Если за предложение работы просят деньги вперёд — это мошенничество. Настоящий
работодатель не требует оплаты от кандидата.

Делайте отдельный пароль для каждого сервиса и по возможности включайте
двухфакторное подтверждение. Не открывайте подозрительные ссылки.
""",
        ),
        "en": (
            "Digital safety: protecting yourself from fraud",
            """
Never give anyone the code from an SMS. No bank, portal or government office
will ever ask for it. Anyone who does is a fraudster.

If money is asked for up front in exchange for a job offer, it is fraud. A real
employer does not charge a candidate.

Use a separate password for each service and turn on two-factor confirmation
where you can. Do not open suspicious links.
""",
        ),
    },
    "Diagnostikadan qayta oʻtish va natijani yaxshilash": {
        "ru": (
            "Повторная диагностика и улучшение результата",
            """
Диагностику можно пройти повторно, и это рекомендуется — обычно раз в три
месяца. Первый результат сохраняется как базовый, последующие сравниваются с
ним.

Не пытайтесь искусственно завысить балл. Диагностика нужна не для оценки вас, а
чтобы найти подходящее направление. Честный ответ даёт более точный план.

Чтобы балл рос, выполняйте шаги плана: завершайте программы, добавляйте навыки,
подавайте заявки на возможности. Каждый выполненный шаг отражается в
соответствующем измерении.
""",
        ),
        "en": (
            "Retaking the diagnostic and improving your result",
            """
You can retake the diagnostic, and it is recommended — usually once every three
months. Your first result is kept as the baseline and later results are compared
against it.

Do not try to inflate the score. The diagnostic exists to find the right
direction for you, not to grade you. An honest answer produces a better plan.

To move the score, work through your plan: finish programmes, add skills, apply
for opportunities. Every completed step shows up in the dimension it belongs to.
""",
        ),
    },
    "AI yordamchi qanday ishlaydi va nimani bilmaydi": {
        "ru": (
            "Как работает AI-помощник и чего он не знает",
            """
AI-помощник WomanUP отвечает на три вида вопросов: о портале (как
зарегистрироваться, что такое Development Score, как устроены курсы и
возможности), об образовании и выборе профессии, а также даёт соответствующую
возрасту общую информацию о здоровье. Ответы о портале берутся только из
утверждённых документов; помощник не выдумывает то, чего не знает, и прямо
говорит «не знаю».

Помощник не ставит диагнозов, не назначает лечение и не даёт юридических
заключений — в таких вопросах он советует обратиться к специалисту. Обращение о
насилии, опасности или мошенничестве сразу передаётся координатору.

Без аккаунта можно задать три вопроса; после регистрации ограничений нет, и
помощник опирается на ваш результат диагностики, курсы и план. У каждого ответа
указан источник, на который он опирается.
""",
        ),
        "en": (
            "How the AI assistant works and what it does not know",
            """
The WomanUP AI assistant answers three kinds of question: about the portal (how
to register, what the Development Score is, how courses and opportunities work),
about education and choosing a profession, and age-appropriate general health
information. Answers about the portal come only from approved documents; the
assistant does not invent what it does not know and says so plainly.

It does not diagnose, prescribe or give legal opinions — for those it points you
to a professional. A message about violence, danger or fraud is passed to a
coordinator straight away.

Without an account you can ask three questions; once registered there is no
limit, and the assistant draws on your diagnostic result, courses and plan. Every
answer shows the source it rests on.
""",
        ),
    },
}


KNOWLEDGE = [
    (
        "WomanUP portali nima va u qanday ishlaydi",
        "faq",
        """
WomanUP — ayol-qizlar uchun milliy raqamli rivojlanish platformasi. Portal sizni
roʻyxatdan oʻtkazadi, ehtiyojlaringizni aniqlaydi, individual rivojlanish rejasini
tuzadi va sizni real imkoniyatlarga ulaydi.

Portaldagi yoʻl beshta bosqichdan iborat. Birinchidan, siz elektron pochta va parol bilan
(yoki Google hisobingiz orqali) roʻyxatdan oʻtasiz va WomanUP ID olasiz. Ikkinchidan, 10-15 daqiqalik diagnostikadan
oʻtasiz. Uchinchidan, tizim sizga Development Score va individual reja taklif qiladi.
Toʻrtinchidan, siz dasturlarga yozilasiz va oʻqishni boshlaysiz. Beshinchidan, mos
imkoniyatlar - vakansiya, grant yoki savdo - sizga tavsiya qilinadi.

Portaldan foydalanish bepul. Asosiy xizmatlar barcha foydalanuvchilar uchun ochiq.
""",
    ),
    (
        "Development Score qanday hisoblanadi",
        "guideline",
        """
WomanUP Development Score - bu 0 dan 100 gacha boʻlgan kompozit koʻrsatkich. U
sakkizta oʻlchov boʻyicha hisoblanadi: taʼlim va koʻnikmalar, karyera va bandlik,
tadbirkorlik, moliyaviy savodxonlik, raqamli koʻnikmalar, salomatlik va muvozanat,
oila va munosabatlar, yetakchilik va global imkoniyatlar. Diagnostika 25 savoldan
iborat va 10-15 daqiqa oladi.

Har bir oʻlchov uchun uchta qiymat saqlanadi: baseline - birinchi diagnostikadagi
natijangiz, current - hozirgi natijangiz, target - maqsadli qiymat. Progress
mutlaq idealga emas, aynan sizning boshlangʻich nuqtangizga nisbatan oʻlchanadi.

Ball past boʻlishi muammo emas. U shunchaki qayerdan boshlashni koʻrsatadi.
Diagnostikani istalgan vaqtda qayta topshirishingiz mumkin.
""",
    ),
    (
        "Shaxsiy maʼlumotlaringiz qanday himoyalanadi",
        "legal",
        """
WomanUP shaxsiy maʼlumotlarni minimal hajmda yigʻadi va faqat sizning roziligingiz
asosida ishlatadi.

Hamkor platformalarga - Edu-Job, Invest HUB va Tijorat markaziga - maʼlumot
yuborilishidan oldin sizdan alohida rozilik soʻraladi. Rozilik bermasangiz, hech
qanday maʼlumot yuborilmaydi. Roziligingizni istalgan vaqtda qaytarib olishingiz
mumkin.

Hamkorlarga telefon raqamingiz, elektron pochtangiz, familiya-ismingiz, oilaviy
holatingiz va farzandlaringiz soni yuborilmaydi. Faqat psevdonim identifikator va
kasbiy maʼlumotlar - taʼlim, kasb, koʻnikmalar - almashiladi.

Har bir maʼlumot almashish hodisasi audit jurnaliga yoziladi. Hisobingizni
oʻchirishingiz mumkin: identifikatorlar tozalanadi, faoliyat tarixi anonim qoladi.
""",
    ),
    (
        "Kursga qanday yozilaman va sertifikat olamanmi",
        "faq",
        """
Dasturlar katalogidan kursni tanlang va "Yozilish" tugmasini bosing. Kurs shaxsiy
kabinetingizdagi "Mening kurslarim" boʻlimida paydo boʻladi.

Har bir kurs modullardan iborat. Modulni tugatganingizda progress avtomatik
yangilanadi. Barcha modullar tugaganda kurs "yakunlangan" holatiga oʻtadi.

Agar kursda sertifikat koʻzda tutilgan boʻlsa, u yakunlanganda avtomatik beriladi.
Har bir sertifikatda noyob raqam va tekshirish kodi boʻladi - ish beruvchi bu kod
orqali sertifikatning haqiqiyligini hisobsiz ham tekshira oladi.
""",
    ),
    (
        "Ishga joylashish uchun nima qilishim kerak",
        "guideline",
        """
Portal taʼlimdan ishga oʻtish mexanizmini Edu-Job platformasi bilan integratsiya
orqali taʼminlaydi.

Avval profilingizni toʻldiring va koʻnikmalaringizni kiriting - tavsiyalar aynan
shu maʼlumotga tayanadi. Keyin "Imkoniyatlar" boʻlimiga oʻting. Har bir vakansiya
uchun tizim sizning koʻnikmalaringiz bilan talab qilinganlarini solishtiradi va
qaysi koʻnikma yetishmayotganini koʻrsatadi.

Yetishmayotgan koʻnikma boʻlsa, tizim uni beradigan kursni tavsiya qiladi. Bu
"skill-gap tahlili" deb ataladi. Kursni tugatib, arizani qayta yuborishingiz mumkin.

Ariza yuborish uchun Edu-Job bilan maʼlumot almashishga rozilik berishingiz kerak.
Ariza holati shaxsiy kabinetingizda kuzatiladi.
""",
    ),
    (
        "Biznes ochish uchun qanday yordam bor",
        "guideline",
        """
Invest HUB integratsiyasi tadbirkorlik yoʻlidagi ayollar uchun moʻljallangan.

Portalda tadbirkorlik yoʻnalishidagi dasturlarni tugatganingizdan soʻng sizga
grant, imtiyozli kredit, akselerator va mentor imkoniyatlari tavsiya qilinadi.

Biznes-reja va moliyaviy modelni tayyorlashda "Tadbirkorlik asoslari" kursi yordam
beradi. Ariza yuborilgandan keyin uning holati kabinetingizda koʻrinadi.

Agar mahsulot yoki xizmat sotmoqchi boʻlsangiz, Tijorat markazi orqali sotuvchi
profilini ochishingiz va raqamli vitrina yaratishingiz mumkin.
""",
    ),
    (
        "Dasturga qanday yozilish va sertifikat olish mumkin",
        "faq",
        """
Dasturga yozilish uchun katalogdan mos dasturni tanlang va "Yozilish" tugmasini
bosing. Yozilgandan keyin dastur sizning kabinetingizda "Mening dasturlarim"
boʻlimida paydo boʻladi va siz istalgan vaqtda davom ettirishingiz mumkin.

Har bir dastur modullardan iborat. Modulni tugatganingizda progress avtomatik
saqlanadi. Dastur 100 foizga yetganda sertifikat avtomatik beriladi. Sertifikat kabinetdan yuklab olinadi.

Barcha dasturlarda sertifikat boʻlavermaydi - dastur kartochkasida bu alohida
koʻrsatilgan. Sertifikatsiz dasturlar ham rejangizda hisobga olinadi.
""",
    ),
    (
        "Individual rivojlanish rejasi qanday tuziladi",
        "guideline",
        """
Reja diagnostika natijalari asosida tuziladi. Tizim eng past ball toʻplangan
oʻlchovlarni aniqlaydi va shu yoʻnalishlar boʻyicha aniq qadamlar taklif qiladi.
Har bir qadam dastur yoki imkoniyat bilan bogʻlanishi mumkin.

Muhim qoida: reja avtomatik kuchga kirmaydi. Tizim faqat taklif qiladi, qaror
sizniki. Rejani koʻrib chiqasiz va "Qabul qilish" tugmasi orqali tasdiqlaysiz.
Tasdiqlanmagan reja faol hisoblanmaydi.

Rejani istalgan vaqtda qayta yaratish mumkin - masalan, yangi diagnostikadan
keyin yoki maqsadingiz oʻzgarganda. Eski reja tarixda saqlanib qoladi.
""",
    ),
    (
        "Mening maʼlumotlarim kimga va qachon uzatiladi",
        "policy",
        """
Sizning maʼlumotlaringiz hech qachon sizning roziligingizsiz portaldan tashqariga
chiqmaydi. Har bir hamkor tizim - Edu-Job, Invest HUB, Tijorat Markazi - uchun
alohida rozilik soʻraladi.

Rozilik berganingizda ham hamkorga faqat minimal maʼlumot uzatiladi: pseudonim
identifikator, viloyat, kasbiy koʻnikmalar va taʼlim darajasi. Telefon raqami,
toʻliq ism, oilaviy holat va farzandlar soni hech qachon uzatilmaydi.

Roziligingizni istalgan vaqtda qaytarib olishingiz mumkin - kabinetdagi
"Rozilik" boʻlimidan. Qaytarib olingandan keyin yangi uzatishlar toʻxtaydi.
""",
    ),
    (
        "Imkoniyatga ariza berish tartibi",
        "faq",
        """
Imkoniyatlar boʻlimida sizga mos vakansiya, grant yoki savdo maydoni tavsiya
qilinadi. Moslik sizning koʻnikmalaringiz, viloyatingiz va rivojlanish
rejangiz asosida hisoblanadi.

Ariza berish uchun avval tegishli hamkor tizimga maʼlumot uzatishga rozilik
berishingiz kerak. Rozilik boʻlmasa, tizim arizani qabul qilmaydi - bu himoya
choralari.

Ariza holati kabinetda kuzatiladi: yuborilgan, koʻrib chiqilmoqda, qabul
qilingan yoki rad etilgan. Hamkor tizimdan javob kelganda sizga bildirishnoma
yuboriladi.
""",
    ),
    (
        "Mentor bilan qanday ishlash kerak",
        "guideline",
        """
Mentor - bu sizga oʻz tajribasini ulashadigan amaliyotchi. Mentor sizning
oʻrningizga qaror qabul qilmaydi va ish topib bermaydi; u yoʻlni koʻrsatadi.

Birinchi uchrashuvga tayyorlaning: qaysi savolga javob olmoqchisiz va qanday
natijani kutayotganingizni yozib qoʻying. Uchrashuv odatda 45-60 daqiqa davom
etadi.

Mentorlik bepul va ixtiyoriy. Agar mos kelmasa, boshqa mentorni tanlashingiz
mumkin - bu normal holat.
""",
    ),
    (
        "Mehnat huquqlari: dekret, taʼtil va shartnoma",
        "guideline",
        """
Mehnat shartnomasi yozma shaklda tuzilishi shart. Shartnomada lavozim, ish haqi,
ish vaqti va taʼtil muddati koʻrsatiladi. Ogʻzaki kelishuv sizni himoya qilmaydi.

Yillik haq toʻlanadigan taʼtil kamida 15 ish kunini tashkil qiladi. Homiladorlik
va tugʻish taʼtili qonunda alohida belgilangan va uni bekor qilib boʻlmaydi.

Ish beruvchi homiladorlik sababli ishga qabul qilishni rad eta olmaydi va
shartnomani bekor qila olmaydi. Huquqingiz buzilsa, mehnat inspeksiyasiga
murojaat qilishingiz mumkin.

Bu umumiy maʼlumot. Aniq holat boʻyicha yurist bilan maslahatlashing.
""",
    ),
    (
        "Raqamli xavfsizlik: firibgarlikdan himoya",
        "guideline",
        """
Hech kimga SMS orqali kelgan kodni aytmang. Bank, portal yoki davlat idorasi
hech qachon kodni soʻramaydi. Kodni soʻrayotgan har qanday odam - firibgar.

Ish taklifi uchun oldindan pul soʻralsa, bu firibgarlik. Haqiqiy ish beruvchi
nomzoddan toʻlov talab qilmaydi.

Parolni har bir xizmat uchun alohida qiling va imkon boʻlsa ikki bosqichli
tasdiqlashni yoqing. Shubhali havolalarni ochmang.
""",
    ),
    (
        "Diagnostikadan qayta oʻtish va natijani yaxshilash",
        "faq",
        """
Diagnostikadan qayta oʻtish mumkin va bu tavsiya etiladi - odatda uch oyda bir
marta. Birinchi natija bazaviy koʻrsatkich sifatida saqlanadi, keyingi natijalar
u bilan solishtiriladi.

Ballni sunʼiy oshirishga urinmang. Diagnostika sizni baholash uchun emas, sizga
mos yoʻnalishni topish uchun kerak. Haqiqiy javob aniqroq reja beradi.

Ball oʻsishi uchun rejadagi qadamlarni bajaring: dasturni tugating, koʻnikma
qoʻshing, imkoniyatga ariza bering. Har bir bajarilgan qadam tegishli oʻlchovda
aks etadi.
""",
    ),
    (
        "AI yordamchi qanday ishlaydi va nimani bilmaydi",
        "faq",
        """
WomanUP AI yordamchisi uchta turdagi savolga javob beradi: portal haqida (qanday
roʻyxatdan oʻtish, Development Score nima, kurslar va imkoniyatlar qanday ishlaydi),
taʼlim va kasb tanlash boʻyicha hamda umumiy salomatlik mavzularida yoshga mos
maʼlumot. Portal haqidagi javoblar faqat tasdiqlangan hujjatlardan olinadi;
yordamchi bilmagan narsasini oʻylab topmaydi va "bilmayman" deyishdan tortinmaydi.

Yordamchi tashxis qoʻymaydi, davolash yoki huquqiy xulosa bermaydi — bunday
savollarda u mutaxassisga murojaat qilishni maslahat beradi. Zoʻravonlik, xavf yoki
firibgarlik haqidagi murojaat darhol koordinatorga yoʻnaltiriladi.

Akkauntsiz uchta savol berish mumkin; roʻyxatdan oʻtgach, cheklov yoʻq, va
yordamchi sizning diagnostika natijangiz, kurslaringiz va rejangizga tayanib javob
beradi. Har bir javob qaysi manbaga asoslangani koʻrsatiladi.
""",
    ),
]
