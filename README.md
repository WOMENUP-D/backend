# WomanUP — Backend (API)

Milliy raqamli rivojlantirish ekotizimi. Ayol-qizlarni har tomonlama rivojlantirish milliy dasturi, 1-bosqich: **WomanUP portali**.

> Har bir ayol uchun individual rivojlanish trayektoriyasi.

Bu repozitoriyada portalning **API qismi** joylashgan: FastAPI, PostgreSQL 16 +
pgvector, SQLAlchemy 2.0 (async), Alembic.

Veb-ilova alohida repozitoriyada: https://github.com/WOMENUP-D/frontend

Texnik topshiriq (TZ) va strategik hujjat ochiq repozitoriyaga chiqarilmagan.

---

## Nima qilinadi

Foydalanuvchi yo'li — bitta uzluksiz sikl:

```
WomanUP ID → Diagnostika → Development Score → AI reja → Dasturlar
     → Imkoniyatlar (Edu-Job / Invest HUB / Tijorat markazi) → Natija → KPI
```

| Blok | Holati |
|------|--------|
| OTP ro'yxatdan o'tish, JWT, 8 rolli RBAC | ✅ ishlaydi |
| Profil, maqsadlar, consent boshqaruvi | ✅ ishlaydi |
| Diagnostika + Development Score (8 o'lchov, 0–100) | ✅ ishlaydi |
| AI individual reja + rule-based fallback | ✅ ishlaydi |
| Dasturlar katalogi, enrollment, progress, sertifikat | ✅ ishlaydi |
| AI Navigator (RAG + guardrails + eskalatsiya) | ✅ ishlaydi (bilim bazasi to'ldirilishi kerak) |
| Imkoniyatlar, arizalar, skill-gap tahlili | ✅ ishlaydi |
| Mentorlik | ✅ ishlaydi |
| Integratsiya gateway (outbox + consent gate) | ✅ ishlaydi (hamkor API'lari ulanishi kerak) |
| Notification engine (in-app / email / SMS / push) | ✅ ishlaydi (provayder adapterlari kerak) |
| Admin dashboard, KPI, CSV eksport, audit | ✅ ishlaydi |
| Frontend | 🟡 skelet (API klienti tayyor, ekranlar yo'q) |

---

## Demo versiyani ishga tushirish

```bash
# 1. Baza (PostgreSQL 16 + pgvector kerak)
createdb womanup && psql -d womanup -c "CREATE EXTENSION vector;"

# 2. API
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # DATABASE_URL va JWT_SECRET_KEY ni to'ldiring
alembic upgrade head
python -m app.seed          # demo ma'lumotlar
uvicorn app.main:app --port 8000
```

API hujjati: **http://localhost:8000/docs**

Veb-ilovani ishga tushirish uchun https://github.com/WOMENUP-D/frontend ga qarang — u shu API'ga
`NEXT_PUBLIC_API_URL` orqali ulanadi.

### Demo hisoblari

| Rol | Telefon | Nimani ko'rsatadi |
|---|---|---|
| Foydalanuvchi | `+998900000002` | Diagnostika topshirilgan, ball hisoblangan, kurs boshlangan |
| Administrator | `+998900000001` | Boshqaruv paneli, KPI, hududiy qamrov |

SMS provayderi ulanmagani uchun tasdiqlash kodi demo rejimida ekranning o'zida
ko'rsatiladi. Istalgan boshqa `+998` raqami bilan yangi hisob ochish ham mumkin —
u holda diagnostikadan boshlab butun yo'lni o'tasiz.

### Demo'da nimani ko'rsatish mumkin

1. **Kirish** — telefon raqami va bir martalik kod, parolsiz.
2. **Diagnostika** — 24 savol, 8 o'lchov; yakunda Development Score va eng zaif yo'nalishlar.
3. **Individual reja** — tizim reja taklif qiladi; u **siz tasdiqlaguningizcha faol emas**.
4. **Dasturlar** — 12 kurs, filtr va qidiruv, kartochka standarti, kursga yozilish.
5. **Imkoniyatlar** — 10 ta vakansiya/grant/savdo; ko'nikma mosligi foizi va yetishmayotgan ko'nikmalar.
6. **Rozilik to'sig'i** — rozilik bermasdan ariza yuborib bo'lmaydi (API 403 qaytaradi).
7. **AI Navigator** — javob faqat tasdiqlangan manbalardan; zo'ravonlik mavzusi modelga umuman yuborilmay, odamga eskalatsiya qilinadi.
8. **RBAC** — oddiy foydalanuvchi `/admin` ga kira olmaydi (403).
9. **Boshqaruv paneli** — 128 foydalanuvchi, 14 hudud, MVP KPI to'plami.

### Demo cheklovlari — ochiq aytilgan

| Cheklov | Sabab |
|---|---|
| SMS yuborilmaydi, kod ekranda | Provayder shartnomasi yo'q |
| AI matn generatsiya qilmaydi | `ANTHROPIC_API_KEY` sozlanmagan. Navigator tasdiqlangan manbadan **to'g'ridan-to'g'ri parcha** keltiradi, hech narsa o'ylab topilmaydi. Reja qoidalar asosida tuziladi |
| Hamkor platformalar mock | Edu-Job, Invest HUB va Tijorat markazining sandbox URL/kalitlari yo'q. Arizalar outbox'ga yoziladi va yetkazishga urinadi |
| Statistika shartli | 128 foydalanuvchi generatsiya qilingan, real ma'lumot emas |

`ANTHROPIC_API_KEY` ni `.env` ga qo'shsangiz, AI reja va navigator to'liq
generativ rejimda ishlaydi — kodda hech narsa o'zgartirish shart emas.

---

## Arxitektura

```
backend/
├── app/
│   ├── main.py             app factory, middleware, error handlers
│   ├── worker.py           background worker (outbox, notifications, risk flags)
│   ├── db.py               engine + session
│   ├── api/                router: bitta resurs — bitta fayl
│   ├── core/               config, security, constants, logging
│   ├── models/             SQLAlchemy models
│   ├── schemas/            Pydantic request/response
│   └── services/           biznes mantiq
├── alembic/                migratsiyalar
├── tests/
├── Dockerfile
└── .github/workflows/ci.yml
```

**Prinsip:** router'lar yupqa, qoidalar `services/` ichida. AI faqat `llm_gateway.py` orqali chiqadi — PII tozalash, versiyalash va trace bitta joyda.

---

## Ishga tushirish

### Docker orqali

```bash
cp .env.example .env   # JWT_SECRET_KEY ni generatsiya qiling
docker build -t womanup-backend .
docker run --env-file .env -p 8000:8000 womanup-backend
```

Baza va veb-ilova bilan birga ko'tarish uchun `docker-compose.yml` kerak — u
ikkala servisni ham tavsiflaydi va shu sababli bu repozitoriyada emas.

- API: http://localhost:8000 · Swagger: http://localhost:8000/docs

### Lokal (Docker'siz)

Talab: Python 3.12+, PostgreSQL 16 + **pgvector**.

```bash
# pgvector (macOS):  brew install pgvector
createdb womanup && psql -d womanup -c "CREATE EXTENSION vector;"

python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
python -c "import secrets; print('JWT_SECRET_KEY=' + secrets.token_urlsafe(64))" >> .env

alembic upgrade head
uvicorn app.main:app --reload          # API
python -m app.worker                   # alohida terminalda
```

### Testlar

```bash
createdb womanup_test && psql -d womanup_test -c "CREATE EXTENSION vector;"
TEST_DATABASE_URL="postgresql+asyncpg://USER@localhost:5432/womanup_test" pytest
ruff check app tests
```

Modellar PostgreSQL'ga xos tiplardan (JSONB, ARRAY, INET, pgvector) foydalanadi. `TEST_DATABASE_URL` mavjud bo'lmasa, bazaga bog'liq testlar `skip` qilinadi, mantiqiy testlar baribir ishlaydi.

---

## Maxfiylik va xavfsizlik

«Shaxsga doir ma'lumotlar to'g'risida»gi qonun talablari kodga o'rnatilgan:

- **Consent gate** — hech qanday ma'lumot hamkor platformaga rozilik yozuvisiz chiqmaydi (`integration_gateway.queue_event` `ConsentMissingError` beradi).
- **Data minimisation** — hamkorlarga telefon, email, F.I.Sh., oilaviy holat va farzandlar soni **yuborilmaydi**; faqat psevdonim `womanup_id` va kasbiy maydonlar (`minimal_profile_payload`).
- **Append-only consent** — rozilikni qaytarib olish yangi yozuv qo'shadi, eskisi o'chirilmaydi; joriy holat = eng oxirgi yozuv.
- **PII scrubbing** — telefon, email, PINFL va karta raqamlari log'ga va AI prompt'iga tushishidan oldin tozalanadi.
- **Audit log** — insert-only; production'da bu jadvalga UPDATE/DELETE huquqi berilmasligi kerak.
- **Sensitive maydonlar** — `ProfileRead` sxemasida umuman yo'q, shuning uchun xodimlar marshruti orqali sizib chiqa olmaydi.
- **O'chirish huquqi** — `DELETE /users/me` identifikatorlarni tozalaydi, faoliyat tarixi anonim holda qoladi (KPI buzilmaydi).

### AI guardrails (TZ 06-bo'lim)

- Javoblar **faqat tasdiqlangan** bilim bazasidan (`is_approved=True` chunk'lar).
- Zo'ravonlik, o'ziga zarar, firibgarlik mavzulari **modelga yuborilmasdan** to'g'ridan-to'g'ri odamga eskalatsiya qilinadi.
- `AI_MIN_CONFIDENCE` dan past ishonchda tizim taxmin qilmaydi — «bilmayman» deydi.
- Har bir chaqiruv `ai_interactions` ga yoziladi: model versiyasi, prompt versiyasi, trace_id, manbalar, ishonch.
- AI **hech qachon qaror qabul qilmaydi**: reja foydalanuvchi tasdiqlaguncha faol emas; risk flag faqat koordinatorga signal, avtomatik sanksiya yo'q.

---

## Muhim texnik qarorlar

| Qaror | Sabab |
|---|---|
| Transactional outbox (`integration_events`) | Hamkor platforma ishlamayotganida foydalanuvchi arizasi yo'qolmaydi |
| Idempotency key hamma integratsiyada | Hamkor bir xil batch'ni qayta yuborsa dublikat yaratilmaydi |
| Enum qiymatlari `values_callable` bilan | Bazada `education_skills`, `EDUCATION_SKILLS` emas — API bilan mos |
| `str_enum` = `native_enum=False` | Yangi qiymat qo'shish uchun PostgreSQL enum migratsiyasi shart emas |
| Diagnostika savollari versiyalanadi | Instrument o'zgarsa ham eski ballar qayta hisoblanadi |
| Development Score: baseline / current / target | O'sish mutlaq idealga emas, boshlang'ich nuqtaga nisbatan o'lchanadi |
| AI ishlamasa — rule-based fallback reja | Xato ekrani o'rniga ishlaydigan reja |
| RAG vector bo'lmasa full-text search'ga tushadi | Embedding provayderi uzilganda navigator ishlashda davom etadi |

---

## Keyingi qadamlar

1. **SMS/email provayderi** — `notification_service.LoggingAdapter` o'rniga real adapter (pilotgacha majburiy).
2. **Embedding provayderi** — `llm_gateway.embed()` hozir `NotImplementedError`; data-residency yuridik tekshiruvidan keyin ulanadi.
3. **Bilim bazasi** — navigator javob berishi uchun tasdiqlangan kontent yuklanishi kerak (`POST /ai/knowledge` → `POST /ai/knowledge/{id}/approve`).
4. **Hamkor API kontraktlari** — Edu-Job, Invest HUB, Tijorat markazi bilan sandbox URL va OAuth2 client credentials.
5. **Diagnostika savollari** — `assessment_questions` jadvali bo'sh; metodologiya bilan to'ldirilishi kerak.
6. **Frontend** — 23 ta MVP ekrani, design system.
7. **Xavfsizlik qattiqlashtirish** — JWT revocation denylist (Redis), rate limiting, pentest.

Batafsil: [docs/architecture.md](docs/architecture.md) · [docs/data-model.md](docs/data-model.md) · [docs/api.md](docs/api.md)

---

## Boshqaruv

Loyiha boshqaruvchisi — **Durdona** (biznes talablar, prioritet, MVP qabuli).
Institutsional uy — O'zbekiston iqtisodiyot assambleyasi.
