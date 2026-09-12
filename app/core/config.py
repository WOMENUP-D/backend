"""Application settings. Every secret comes from the environment — never from code."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
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

    # --- Object storage -------------------------------------------------
    s3_endpoint_url: str | None = None
    s3_bucket: str = "womanup-media"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None

    # --- Security -------------------------------------------------------
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    rate_limit_per_minute: int = 120

    @field_validator(
        "cors_origins",
        "supported_languages",
        "news_ingest_allowed_domains",
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
    return Settings()


settings = get_settings()
