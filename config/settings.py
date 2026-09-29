"""Central configuration. All values load from .env - never hard-code secrets here."""
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

ALLOWED_DATA_SOURCES = ("sqlserver", "local")
ALLOWED_LLM_PROVIDERS = ("ollama", "gemini")
ALLOWED_AUTH_MODES = ("csv", "sso")
# Anything shorter is guessable, and the secret is all that stops a direct POST to the
# backend from claiming someone else's SSO identity.
SSO_SECRET_MIN_LENGTH = 16
# What scripts/build_oauth2_proxy_config.py needs to write a working oauth2-proxy config.
ENTRA_SETTINGS = ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET",
                  "ENTRA_REDIRECT_URI", "SSO_COOKIE_SECRET")

# Line and scatter were removed deliberately (see backend/agents/chart_agent.py). Lives
# here rather than in the chart agent so settings validation needs no plotly import.
ALLOWED_CHART_TYPES = ("bar", "pie", "donut")
_CHART_TYPE_SYNONYMS = {
    "bar chart": "bar", "column": "bar", "column chart": "bar",
    "pie chart": "pie",
    "donut chart": "donut", "doughnut": "donut", "doughnut chart": "donut",
}
# Written the way people actually type a list into .env: "bar, pie chart and donut".
_CHART_TYPE_SEPARATOR_RE = re.compile(r"[,;|/+&]|\band\b")

# Local-file extensions the "local" data source can read.
LOCAL_CSV_SUFFIXES = (".csv", ".txt", ".tsv")
LOCAL_EXCEL_SUFFIXES = (".xlsx", ".xlsm", ".xls")


class ConfigurationError(RuntimeError):
    """Raised at startup when .env holds a value the app cannot run with."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # ---------------- Data source selection ----------------
    # "sqlserver" -> enterprise SSMS via ODBC.
    # "local"     -> a local .csv or .xlsx via DuckDB.
    # Exactly one is ever initialised (see backend/core/datasource.get_datasource).
    DATA_SOURCE: str = "local"
    LOCAL_DATA_PATH: str = "F:/data/GEN AI PROJECTS/UNIVERSAL SOMPO/dummy_insurance_data.csv"
    # Only meaningful for .xlsx sources; ignored for .csv. Empty -> first sheet.
    LOCAL_SHEET_NAME: str = ""

    # SQL Server
    DB_DRIVER: str = "ODBC Driver 17 for SQL Server"
    DB_SERVER: str = ""
    DB_DATABASE: str = ""
    DB_TABLE: str = "dbo.May_2"
    DB_USERNAME: str = ""
    DB_PASSWORD: str = ""
    DB_CONNECT_TIMEOUT: int = 10
    DB_QUERY_TIMEOUT: int = 30
    DB_MAX_ROWS: int = 5000

    # ---------------- LLM provider selection ----------------
    # "ollama" -> fully local/on-prem, no data leaves the machine.
    # "gemini" -> Google Gemini over HTTPS; needs GEMINI_API_KEY, and question text plus
    #             query RESULTS are sent to Google. Do not use it with production data
    #             unless that has been cleared.
    # Exactly one is ever initialised (see backend/core/llm_provider.get_provider).
    LLM_PROVIDER: str = "ollama"

    # Ollama
    OLLAMA_HOST: str = "http://localhost:11434"
    # SQL and insight default to the SAME model so only one set of 7B weights stays
    # resident - sized for a 16 GB CPU-only host. On a larger box, point
    # OLLAMA_INSIGHT_MODEL at a general instruct model (e.g. qwen2.5:7b) for better prose.
    OLLAMA_SQL_MODEL: str = "qwen2.5-coder:7b"
    OLLAMA_INSIGHT_MODEL: str = "qwen2.5-coder:7b"
    OLLAMA_ROUTER_MODEL: str = "qwen2.5-coder:3b"
    OLLAMA_EMBED_MODEL: str = "nomic-embed-text"
    OLLAMA_REQUEST_TIMEOUT: int = 300
    OLLAMA_CONNECT_TIMEOUT: int = 5
    OLLAMA_RETRY_COUNT: int = 1
    # Keeping the model resident removes the ~2 minute cold-load that made short questions
    # look like the LLM was unreachable.
    OLLAMA_KEEP_ALIVE: str = "30m"
    OLLAMA_NUM_CTX: int = 4096
    OLLAMA_NUM_PREDICT: int = 600
    OLLAMA_WARMUP_ON_STARTUP: bool = True

    # Gemini (used only when LLM_PROVIDER=gemini)
    GEMINI_API_KEY: str = ""
    GEMINI_API_BASE: str = "https://generativelanguage.googleapis.com/v1beta"
    # 2.0-flash is the default because it has no "thinking" budget to tune. If you switch
    # to a 2.5 model, also set GEMINI_THINKING_BUDGET=0 - otherwise reasoning tokens eat
    # GEMINI_MAX_OUTPUT_TOKENS and short answers come back empty.
    GEMINI_SQL_MODEL: str = "gemini-2.0-flash"
    GEMINI_INSIGHT_MODEL: str = "gemini-2.0-flash"
    GEMINI_ROUTER_MODEL: str = "gemini-2.0-flash-lite"
    GEMINI_EMBED_MODEL: str = "text-embedding-004"
    GEMINI_REQUEST_TIMEOUT: int = 120
    GEMINI_CONNECT_TIMEOUT: int = 5
    GEMINI_RETRY_COUNT: int = 1
    GEMINI_MAX_OUTPUT_TOKENS: int = 800
    # -1 -> omit thinkingConfig entirely (correct for 1.5/2.0 models, which reject it).
    # 0  -> disable thinking on 2.5 models. >0 -> allow that many reasoning tokens.
    GEMINI_THINKING_BUDGET: int = -1

    # Sampling temperature. Anything that produces or restates a NUMBER must be fully
    # deterministic; only free prose is allowed any variation.
    LLM_SQL_TEMPERATURE: float = 0.0
    LLM_NUMERIC_TEMPERATURE: float = 0.0
    LLM_TEXT_TEMPERATURE: float = 0.2

    # ChromaDB
    CHROMA_PERSIST_DIR: str = "./data/chroma_store"
    CHROMA_SCHEMA_COLLECTION: str = "schema_metadata"
    CHROMA_EXAMPLES_COLLECTION: str = "example_queries"
    CHROMA_TOP_K: int = 8
    CHROMA_TELEMETRY: bool = False

    # Column alias / shortcut dictionary
    COLUMN_ALIASES_FILE: str = "./backend/knowledge/column_aliases.json"
    COLUMN_FUZZY_THRESHOLD: float = 0.86

    # Charts drawn beside every insight.
    #   auto          -> chosen per question (donut for a share-of-total with <=8 slices,
    #                    bar for everything else)
    #   bar|pie|donut -> always that one type
    #   bar,pie,donut -> one chart of EACH listed type, in that order ("all" = all three)
    # A chart type named in the question itself still wins over this default.
    CHART_TYPE: str = "auto"

    # Cache
    CACHE_ENABLED: bool = True
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    CACHE_TTL_SECONDS: int = 21600

    # Backend
    BACKEND_HOST: str = "127.0.0.1"
    BACKEND_PORT: int = 8000
    REQUEST_TIMEOUT_SECONDS: int = 600
    SESSION_TIMEOUT_MINUTES: int = 480
    DEBUG: bool = True
    ALLOWED_ORIGINS: str = "http://localhost:8501"

    # Frontend
    BACKEND_URL: str = "http://127.0.0.1:8000"
    FRONTEND_REQUEST_TIMEOUT_SECONDS: int = 620

    # ---------------- Login switch ----------------
    # "csv" -> the email form: an email listed in USERS_CSV gets in (no password).
    # "sso" -> no form: nginx + oauth2-proxy sign the user in with the company identity
    #          provider and forward who they are. The email form and /auth/login are off.
    # Either way the "developer" role comes only from the role column of USERS_CSV.
    AUTH_MODE: str = "csv"
    # Read by BOTH processes from this .env: Streamlit sends it with the SSO identity and
    # the backend refuses an identity without it, so a direct POST to :8000 cannot pose as
    # someone else. Generate: python -c "import secrets; print(secrets.token_urlsafe(32))"
    SSO_SHARED_SECRET: str = ""
    # false -> anyone the SSO lets in may use the chatbot (as a "user" unless USERS_CSV
    #          makes them a developer). true -> they must also be listed in USERS_CSV.
    SSO_REQUIRE_USERS_CSV: bool = False

    # Microsoft Entra ID app registration. The chatbot never calls Entra itself -
    # oauth2-proxy does - so these only feed scripts/build_oauth2_proxy_config.py, which
    # writes the proxy's config from this .env; nothing is copied by hand.
    ENTRA_TENANT_ID: str = ""
    ENTRA_CLIENT_ID: str = ""
    ENTRA_CLIENT_SECRET: str = ""
    # Must be https://<chatbot-host>/oauth2/callback and registered in Entra exactly so.
    ENTRA_REDIRECT_URI: str = ""
    # oauth2-proxy's own cookie key, 16/24/32 bytes:
    #   python -c "import secrets; print(secrets.token_hex(16))"
    SSO_COOKIE_SECRET: str = ""
    # Comma list of email domains oauth2-proxy lets in; "*" = anyone in the tenant.
    SSO_EMAIL_DOMAINS: str = "*"

    # Data files
    USERS_CSV: str = "./data/users.csv"
    # Chat audit log: one folder per calendar month holding week1..week4 + month CSVs.
    # CHAT_LOG_KEEP_MONTHS counts calendar months INCLUDING the current one; older month
    # folders are deleted automatically.
    CHAT_LOGS_DIR: str = "./data/chat_logs"
    CHAT_LOG_KEEP_MONTHS: int = 2
    # The pre-partition single-file log. Nothing writes it any more; it stays configurable
    # so `init_data_files.py --split-legacy` can import it.
    CHAT_LOGS_CSV: str = "./data/chat_logs.csv"
    SESSIONS_FILE: str = "./data/sessions.json"
    SCHEMA_METADATA_FILE: str = "./data/schema_metadata.json"

    # Logging
    LOG_DIR: str = "./logs"
    LOG_LEVEL: str = "INFO"

    # ---------------- Validation ----------------

    @field_validator("DATA_SOURCE", mode="before")
    @classmethod
    def _normalize_data_source(cls, value):
        return str(value or "").strip().strip("\"'").lower()

    @field_validator("DATA_SOURCE")
    @classmethod
    def _check_data_source(cls, value: str) -> str:
        if value not in ALLOWED_DATA_SOURCES:
            raise ValueError(
                f"DATA_SOURCE must be one of {ALLOWED_DATA_SOURCES}, got '{value}'. "
                "Set DATA_SOURCE=sqlserver (enterprise SSMS) or DATA_SOURCE=local "
                "(read LOCAL_DATA_PATH) in .env."
            )
        return value

    @field_validator("LLM_PROVIDER", mode="before")
    @classmethod
    def _normalize_llm_provider(cls, value):
        return str(value or "").strip().strip("\"'").lower()

    @field_validator("LLM_PROVIDER")
    @classmethod
    def _check_llm_provider(cls, value: str) -> str:
        if value not in ALLOWED_LLM_PROVIDERS:
            raise ValueError(
                f"LLM_PROVIDER must be one of {ALLOWED_LLM_PROVIDERS}, got '{value}'. "
                "Set LLM_PROVIDER=ollama (fully local) or LLM_PROVIDER=gemini "
                "(Google API, needs GEMINI_API_KEY) in .env."
            )
        return value

    @field_validator("CHART_TYPE", mode="before")
    @classmethod
    def _normalize_chart_type(cls, value):
        raw = str(value or "").strip().strip("\"'").lower()
        if raw in ("", "auto"):
            return "auto"
        if raw == "all":
            return ",".join(ALLOWED_CHART_TYPES)
        chosen: list[str] = []
        for part in _CHART_TYPE_SEPARATOR_RE.split(raw):
            part = " ".join(part.split())
            if not part:
                continue
            part = _CHART_TYPE_SYNONYMS.get(part, part)
            if part not in chosen:
                chosen.append(part)
        return ",".join(chosen) or "auto"

    @field_validator("CHART_TYPE")
    @classmethod
    def _check_chart_type(cls, value: str) -> str:
        if value == "auto":
            return value
        unknown = [part for part in value.split(",") if part not in ALLOWED_CHART_TYPES]
        if unknown:
            raise ValueError(
                f"CHART_TYPE has unsupported value(s) {unknown}. Use auto, one of "
                f"{ALLOWED_CHART_TYPES}, a comma list such as bar,pie,donut, or all. "
                "Line and scatter charts were removed deliberately - use bar."
            )
        return value

    @field_validator("AUTH_MODE", mode="before")
    @classmethod
    def _normalize_auth_mode(cls, value):
        return str(value or "").strip().strip("\"'").lower()

    @field_validator("AUTH_MODE")
    @classmethod
    def _check_auth_mode(cls, value: str) -> str:
        if value not in ALLOWED_AUTH_MODES:
            raise ValueError(
                f"AUTH_MODE must be one of {ALLOWED_AUTH_MODES}, got '{value}'. Set "
                "AUTH_MODE=csv (email login against USERS_CSV) or AUTH_MODE=sso (company "
                "sign-in through nginx + oauth2-proxy) in .env."
            )
        return value

    @field_validator("CHAT_LOG_KEEP_MONTHS")
    @classmethod
    def _check_chat_log_keep_months(cls, value: int) -> int:
        if value < 1:
            raise ValueError(
                f"CHAT_LOG_KEEP_MONTHS must be 1 or more (it counts the current month), "
                f"got {value}. Set CHAT_LOG_KEEP_MONTHS=2 in .env to keep this month and "
                "the previous one."
            )
        return value

    @field_validator(
        "LOCAL_DATA_PATH", "LOCAL_SHEET_NAME", "GEMINI_API_KEY", "SSO_SHARED_SECRET",
        "ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET", "ENTRA_REDIRECT_URI",
        "SSO_COOKIE_SECRET", "SSO_EMAIL_DOMAINS",
        mode="before",
    )
    @classmethod
    def _strip_quotes(cls, value):
        return str(value or "").strip().strip("\"'")

    @model_validator(mode="after")
    def _check_active_backend(self):
        """Fail fast on a configuration the selected backend cannot possibly use.

        Only the ACTIVE backend is validated: local mode must not demand SQL Server
        credentials, and SQL Server mode must not demand the local file exist. The same
        rule applies to the LLM provider - Ollama mode never demands a Gemini key.
        """
        if self.DATA_SOURCE == "sqlserver":
            missing = [
                name
                for name in ("DB_SERVER", "DB_DATABASE", "DB_USERNAME", "DB_PASSWORD")
                if not str(getattr(self, name) or "").strip()
            ]
            if missing:
                raise ValueError(
                    "DATA_SOURCE=sqlserver requires these .env values: "
                    + ", ".join(missing)
                )
        else:
            if not str(self.LOCAL_DATA_PATH or "").strip():
                raise ValueError(
                    "DATA_SOURCE=local requires LOCAL_DATA_PATH to point at a .csv or "
                    ".xlsx file."
                )
            suffix = Path(self.LOCAL_DATA_PATH).suffix.lower()
            if suffix not in LOCAL_CSV_SUFFIXES + LOCAL_EXCEL_SUFFIXES:
                raise ValueError(
                    f"LOCAL_DATA_PATH has an unsupported extension '{suffix}'. "
                    f"Supported: {', '.join(LOCAL_CSV_SUFFIXES + LOCAL_EXCEL_SUFFIXES)}."
                )
        if not str(self.DB_TABLE or "").strip():
            raise ValueError("DB_TABLE must be set (e.g. dbo.May_2).")

        if self.LLM_PROVIDER == "gemini" and not str(self.GEMINI_API_KEY or "").strip():
            raise ValueError(
                "LLM_PROVIDER=gemini requires GEMINI_API_KEY in .env. "
                "Get one at https://aistudio.google.com/apikey, or set "
                "LLM_PROVIDER=ollama to run fully locally."
            )

        if self.AUTH_MODE == "sso" and len(self.SSO_SHARED_SECRET) < SSO_SECRET_MIN_LENGTH:
            raise ValueError(
                f"AUTH_MODE=sso requires SSO_SHARED_SECRET in .env, at least "
                f"{SSO_SECRET_MIN_LENGTH} characters (got {len(self.SSO_SHARED_SECRET)}). "
                "Generate one with: python -c \"import secrets; "
                "print(secrets.token_urlsafe(32))\" - the frontend and the backend must "
                "read the same value. Or set AUTH_MODE=csv to use the email login."
            )
        return self

    # ---------------- Helpers ----------------

    def resolved(self, rel_path: str) -> Path:
        p = Path(rel_path)
        return p if p.is_absolute() else (BASE_DIR / p).resolve()

    @property
    def is_sso(self) -> bool:
        return self.AUTH_MODE == "sso"

    def describe_auth(self) -> str:
        if self.is_sso:
            who = ("listed users only" if self.SSO_REQUIRE_USERS_CSV
                   else "anyone the SSO lets in")
            return (f"sso -> identity from nginx/oauth2-proxy, {who}; "
                    f"developer role from {self.USERS_CSV}")
        return f"csv -> email login against {self.USERS_CSV}"

    def entra_missing(self) -> list[str]:
        return [name for name in ENTRA_SETTINGS if not getattr(self, name)]

    @property
    def entra_issuer_url(self) -> str:
        return f"https://login.microsoftonline.com/{self.ENTRA_TENANT_ID}/v2.0"

    @property
    def sso_email_domains(self) -> list[str]:
        return [d.strip().lower() for d in self.SSO_EMAIL_DOMAINS.split(",") if d.strip()] or ["*"]

    @property
    def odbc_connection_string(self) -> str:
        return (
            f"DRIVER={{{self.DB_DRIVER}}};"
            f"SERVER={self.DB_SERVER};"
            f"DATABASE={self.DB_DATABASE};"
            f"UID={self.DB_USERNAME};"
            f"PWD={self.DB_PASSWORD};"
            f"Connection Timeout={self.DB_CONNECT_TIMEOUT};"
        )

    @property
    def chart_types(self) -> tuple[str, ...]:
        """The configured chart types in order; empty means choose per question."""
        return () if self.CHART_TYPE == "auto" else tuple(self.CHART_TYPE.split(","))

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def is_local_source(self) -> bool:
        return self.DATA_SOURCE == "local"

    @property
    def local_suffix(self) -> str:
        return Path(self.LOCAL_DATA_PATH).suffix.lower()

    @property
    def is_local_csv(self) -> bool:
        return self.is_local_source and self.local_suffix in LOCAL_CSV_SUFFIXES

    def describe_source(self) -> str:
        if self.is_local_source:
            if self.is_local_csv:
                return f"local -> {self.LOCAL_DATA_PATH} (csv) as {self.DB_TABLE}"
            sheet = self.LOCAL_SHEET_NAME or "(first sheet)"
            return f"local -> {self.LOCAL_DATA_PATH} [{sheet}] as {self.DB_TABLE}"
        return f"sqlserver -> {self.DB_SERVER}/{self.DB_DATABASE}.{self.DB_TABLE}"

    # -- LLM provider ------------------------------------------------------------------
    # Agents ask for a ROLE (sql / insight / router / embed), never a provider-specific
    # setting, so switching LLM_PROVIDER needs no change anywhere in the pipeline.

    @property
    def is_gemini(self) -> bool:
        return self.LLM_PROVIDER == "gemini"

    @property
    def sql_model(self) -> str:
        return self.GEMINI_SQL_MODEL if self.is_gemini else self.OLLAMA_SQL_MODEL

    @property
    def insight_model(self) -> str:
        return self.GEMINI_INSIGHT_MODEL if self.is_gemini else self.OLLAMA_INSIGHT_MODEL

    @property
    def router_model(self) -> str:
        return self.GEMINI_ROUTER_MODEL if self.is_gemini else self.OLLAMA_ROUTER_MODEL

    @property
    def embed_model(self) -> str:
        return self.GEMINI_EMBED_MODEL if self.is_gemini else self.OLLAMA_EMBED_MODEL

    @property
    def configured_models(self) -> dict[str, str]:
        return {
            "sql": self.sql_model,
            "insight": self.insight_model,
            "router": self.router_model,
            "embed": self.embed_model,
        }

    def describe_llm(self) -> str:
        if self.is_gemini:
            return f"gemini -> {self.GEMINI_API_BASE} (sql={self.sql_model}, embed={self.embed_model})"
        return f"ollama -> {self.OLLAMA_HOST} (sql={self.sql_model}, embed={self.embed_model})"


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except Exception as exc:  # noqa: BLE001 - turn a pydantic dump into one clear message
        raise ConfigurationError(
            "Invalid configuration in .env - the application cannot start.\n"
            f"{exc}"
        ) from exc
