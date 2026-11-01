"""Application settings. Every secret comes from the environment — never from code."""

from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    PostgresDsn,
    RedisDsn,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- General -------------------------------------------------------
    app_name: str = "WomanUP Portal API"
    environment: Literal["local", "staging", "production"] = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    # --- Database ------------------------------------------------------
    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://womanup:womanup@localhost:5432/womanup",
        description="Async SQLAlchemy DSN (asyncpg driver).",
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # --- Cache / queue -------------------------------------------------
    redis_url: RedisDsn = "redis://localhost:6379/0"

    # --- Auth ----------------------------------------------------------
    jwt_secret_key: str = Field(
        default="change-me-in-env",
        description="HS256 signing key. MUST be overridden outside local dev.",
    )
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 30
    # The refresh token travels in an HttpOnly cookie scoped to the auth
    # endpoints, so a script running in the page can never read it. `secure`
    # defaults to on everywhere but local development (plain http there).
    auth_cookie_name: str = "wu_refresh"
    auth_cookie_samesite: Literal["lax", "strict"] = "lax"
    auth_cookie_secure: bool | None = None
    auth_cookie_domain: str | None = None

    # --- Firebase (Google sign-in) -------------------------------------
    # The browser authenticates against Firebase and sends the ID token here;
    # nothing is believed until the Admin SDK has verified that token, so the
    # values below are what makes any of it trustworthy.
    #
    # All three come from one service-account JSON in the Firebase console.
    # The private key is a real secret: it must never reach the frontend and
    # never be committed.
    firebase_project_id: str | None = None
    firebase_client_email: str | None = None
    firebase_private_key: str | None = None
    # Restrict sign-in to one Workspace domain, e.g. "womanup.uz". Empty
    # accepts any Google account, which is what a public portal wants.
    firebase_allowed_domain: str | None = None

    @field_validator("firebase_private_key", mode="before")
    @classmethod
    def unescape_private_key(cls, value: object) -> object:
        """A PEM has newlines; a .env line does not.

        The key is pasted as one line with literal `\n`, the way every hosting
        panel stores it, and is turned back into a real PEM here.
        """
        if isinstance(value, str):
            return value.strip().strip('"').replace("\\n", "\n")
        return value

    otp_length: int = 6
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 60
    # Signing in by one-time code is off: WomanUP opens an account with an
    # e-mail address and a password, and nothing in the portal asks for a
    # code. The flow stays in the codebase, and stays tested, for the day it
    # is wanted again — but a door the product does not use is not left open.
    otp_enabled: bool = False
    # Even with codes switched on, phone stays off until an SMS provider is
    # wired up: without one the code is never delivered, so the only way to
    # "receive" it is to guess it — which would hand out accounts on numbers
    # nobody owns.
    phone_otp_enabled: bool = False

    # --- OTP rate limits -------------------------------------------------
    # Counted in the database (see `services.rate_limit`), so the limits hold
    # across workers and restarts. A window is a fixed hour by default.
    otp_request_ip_limit: int = 20
    otp_request_ip_window_seconds: int = 3600
    otp_request_identifier_limit: int = 5
    otp_request_identifier_window_seconds: int = 3600
    otp_verify_ip_limit: int = 30
    otp_verify_ip_window_seconds: int = 3600
    otp_verify_identifier_limit: int = 15
    otp_verify_identifier_window_seconds: int = 3600
    # Wrong passwords one account may receive in an hour before it is held,
    # whatever addresses the guesses come from. Desk accounts tighter.
    login_failures_per_hour: int = 20
    staff_login_failures_per_hour: int = 10

    # --- Localisation --------------------------------------------------
    default_language: Literal["uz", "ru", "en"] = "uz"
    supported_languages: Annotated[list[str], NoDecode] = ["uz", "ru", "en"]

    # --- AI layer ------------------------------------------------------
    anthropic_api_key: str | None = None
    ai_model: str = "claude-opus-5"
    ai_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    ai_max_tokens: int = 16000
    # How many assistant questions someone gets before signing up. Enough to
    # see what it is, not enough to use it as a free service.
    assistant_guest_questions: int = 3
    ai_rag_top_k: int = 6
    ai_min_confidence: float = 0.35
    embedding_dimensions: int = 1024

    # --- AI news ingestion ----------------------------------------------
    # Off by default, and this one is not a taste question: a fresh checkout,
    # CI and the test suite must never make a paid outbound call on a timer,
    # let alone publish from one. Turning it on is a deployment decision.
    news_ingest_enabled: bool = False
    news_ingest_interval_hours: int = 8
    # Per run. A cap, not a target: the job publishes what clears the gate.
    news_ingest_max_posts: int = 6
    # How recent an article has to be to be worth ingesting at all.
    news_ingest_lookback_hours: int = 48
    # How far back the near-duplicate title check looks.
    news_ingest_dedup_days: int = 30
    # The second half of the publishing decision, separate from the first:
    # `enabled` turns the search on, this decides whether what it finds may
    # reach a reader without a human. True because the feature was asked for
    # as "search and publish"; the safety argument is answered by
    # `news_ingest.publication_gate`, which is code, not by a queue nobody
    # staffs. Set it false to run the same job as a drafting aid instead.
    news_ingest_auto_publish: bool = True
    # An allowlist, not a blocklist. National sources first — this is a portal
    # for women in Uzbekistan — then the international health and science
    # bodies `news_ranking.TRUSTED_SOURCES` already treats as authoritative.
    news_ingest_allowed_domains: Annotated[list[str], NoDecode] = [
        "uza.uz",
        "gov.uz",
        "ssv.uz",
        "minzdrav.uz",
        "stat.uz",
        "lex.uz",
        "who.int",
        "un.org",
        "unwomen.org",
        "unicef.org",
        "unesco.org",
        "unfpa.org",
        "nobelprize.org",
        "thelancet.com",
        "nature.com",
        "science.org",
        "bmj.com",
        "nejm.org",
        "cochrane.org",
        "cdc.gov",
    ]

    # --- Outbound channels ---------------------------------------------
    sms_provider_url: str | None = None
    sms_provider_token: str | None = None
    smtp_host: str | None = None
    # Optional, because a deployment without mail is a supported state — but
    # a value that is not a port is a mistake, not a preference.
    smtp_port: int | None = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    # What a woman sees in the From line. Falls back to the SMTP user.
    smtp_from: str | None = None

    # --- Partner platform integrations ---------------------------------
    edu_job_base_url: str | None = None
    edu_job_client_id: str | None = None
    edu_job_client_secret: str | None = None

    invest_hub_base_url: str | None = None
    invest_hub_client_id: str | None = None
    invest_hub_client_secret: str | None = None

    commerce_base_url: str | None = None
    commerce_client_id: str | None = None
    commerce_client_secret: str | None = None

    integration_timeout_seconds: float = 15.0
    integration_max_retries: int = 3

    # --- hh.uz vacancy feed ---------------------------------------------
    # Read-only: the worker pulls open vacancies for Uzbekistan from the hh
    # API into `opportunities` under source `hh_uz`. Nothing is ever sent
    # back; a woman applies on hh.uz through the listing's own link.
    hh_sync_enabled: bool = False
    hh_api_base_url: str = "https://api.hh.ru"
    # The hh site the listings belong to; the API serves several.
    hh_host: str = "hh.uz"
    # Uzbekistan in the hh area tree.
    hh_area_id: int = 97
    # The API requires an identifying agent in the form `App/version (contact)`.
    hh_user_agent: str = "WomanUP/1.0 (support@womanup.uz)"
    # Vacancy search is not open to anonymous callers any more: an application
    # registered at dev.hh.ru is exchanged for an application token (client
    # credentials). A token issued by hand can be pasted in directly instead.
    hh_client_id: str | None = None
    hh_client_secret: str | None = None
    hh_access_token: str | None = None
    hh_sync_interval_hours: int = 6
    # What is searched for, one query per page of up to 50 results. Russian,
    # because that is the language most hh.uz listings are written in.
    hh_search_queries: Annotated[list[str], NoDecode] = [
        "бухгалтер",
        "менеджер по продажам",
        "администратор",
        "оператор call-центра",
        "учитель",
        "воспитатель",
        "медсестра",
        "продавец-консультант",
        "SMM",
        "маркетолог",
        "дизайнер",
        "HR",
        "офис-менеджер",
        "кассир",
        "повар",
        "швея",
        "переводчик",
        "программист",
        "тестировщик",
        "стажер",
    ]
    hh_sync_pages_per_query: int = 1
    # At most this many *new* vacancies are read in full per run — each needs
    # its own request for the skills and the description.
    hh_sync_max_new: int = 300
    # A vacancy the searches have not returned for this long is taken down.
    hh_sync_stale_days: int = 10

    # --- Vacancy marketplace ---------------------------------------------
    # Which job board `/api/v1/vacancies` reads live. `headhunter` today; a
    # Tijorat Markazi provider registers its name in `services.vacancies`.
    vacancy_provider: str = "headhunter"
    # How long a page of results and one listing are served from cache.
    vacancy_search_cache_seconds: int = 300
    vacancy_detail_cache_seconds: int = 900

    # --- Object storage -------------------------------------------------
    s3_endpoint_url: str | None = None
    s3_bucket: str = "womanup-media"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None

    @field_validator("smtp_port", mode="before")
    @classmethod
    def mail_port(cls, value: object) -> object:
        """Accept a port, accept no mail at all, refuse anything else.

        On 12 September 2026 a mail port that was not a number reached
        production, where it failed inside the migration — after the deploy had
        already stopped the running containers. The deploy now asks before it
        stops anything (`scripts/preflight.py` in the infra repository), and
        this is the check it asks: an unusable value is refused here, so a
        release carrying one never starts serving.

        Refusing is deliberate. Quietly turning mail off instead would leave a
        portal that signs people in by e-mailing them a code unable to do so,
        and nobody would be told why.

        The value never appears in the message: it is somebody's configuration,
        and `get_settings` keeps Pydantic's own rendering of it out of the log.
        """
        if isinstance(value, bool):
            raise ValueError("SMTP_PORT must be a port number between 1 and 65535")
        if value is None:
            return None
        if isinstance(value, int):
            if 1 <= value <= 65535:
                return value
            raise ValueError("SMTP_PORT must be a port number between 1 and 65535")
        # A secret store hands values back with the quotes and spaces it was
        # given; that shape is not a reason to refuse a perfectly good port.
        text = str(value).strip().strip("\"'").strip()
        if not text:
            # Mail is optional: unset stays unset, and the mailer reports
            # itself as not configured.
            return None
        if text.isdigit() and 1 <= int(text) <= 65535:
            return int(text)
        raise ValueError("SMTP_PORT must be a port number between 1 and 65535")

    # --- Security -------------------------------------------------------
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    # Host names the API answers for; anything else is refused before routing.
    # `*` (the default) trusts the proxy to have checked — set it to the
    # site's hosts in production so a forged Host header buys nothing.
    allowed_hosts: Annotated[list[str], NoDecode] = ["*"]
    # Requests per minute one address may make to the API at all, and to the
    # expensive paths (the live job-board proxy, the model-backed endpoints).
    # Per worker and in process — the first line, not the only one; see
    # `core.throttle`. Generous: an office behind one address is not a flood.
    rate_limit_per_minute: int = 600
    expensive_rate_limit_per_minute: int = 60
    # Data kept only as long as it is useful. The assistant's prompt and
    # answer excerpts are personal text and are blanked after this many days
    # (the trace — model, prompt version, id — stays); spent one-time codes
    # and expired throttle rows go after a day.
    ai_trace_retention_days: int = 180
    otp_retention_hours: int = 24
    # The largest request body the API accepts. Nothing here uploads files;
    # the biggest legitimate body is a partner's listing batch.
    max_request_body_bytes: int = 2_000_000
    # How many model-backed answers one account, and one anonymous address,
    # may ask for in an hour. Counted in the database (`services.ai_limits`),
    # so the limit holds across workers and restarts — a cap on what an
    # abusive script can make the portal spend, not a product quota.
    ai_user_hourly_limit: int = 60
    ai_guest_ip_hourly_limit: int = 20

    @model_validator(mode="after")
    def production_is_configured(self) -> Self:
        """Refuse to serve outside local development with placeholder secrets.

        The signing key's default exists so a developer can start the API with
        no `.env` at all. In staging or production that same default would let
        anyone mint an administrator's token, so the process must not start
        with it — and must not start in debug mode, which prints tracebacks
        to clients. The values never appear in the message.
        """
        if self.environment == "local":
            return self
        problems: list[str] = []
        if self.jwt_secret_key == "change-me-in-env" or len(self.jwt_secret_key) < 32:
            problems.append("JWT_SECRET_KEY must be set to at least 32 random characters")
        if self.debug:
            problems.append("DEBUG must be false")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @field_validator(
        "cors_origins",
        "supported_languages",
        "news_ingest_allowed_domains",
        "hh_search_queries",
        "allowed_hosts",
        mode="before",
    )
    @classmethod
    def split_list(cls, value: object) -> object:
        """Accept both JSON (`["a","b"]`) and comma-separated (`a,b`) forms.

        A plain `.env` sourced through a shell loses the JSON quoting, so the
        comma form has to work too.
        """
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return value
            return [item.strip() for item in text.split(",") if item.strip()]
        return value

    @property
    def firebase_enabled(self) -> bool:
        """True only when a real service account is configured."""
        return bool(
            self.firebase_project_id and self.firebase_client_email and self.firebase_private_key
        )

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """The settings, or a refusal that names the setting and nothing else.

    Pydantic prints the offending input in its own error, which is how a
    configuration value reaches a log — an Actions log, a container log, a
    screenshot in a chat. The names are enough to fix it.
    """
    try:
        return Settings()
    except ValidationError as exc:
        names = sorted({str(error["loc"][0]) for error in exc.errors()})
        raise RuntimeError(
            f"Invalid configuration: {', '.join(names)} (values are not shown)"
        ) from None


settings = get_settings()
