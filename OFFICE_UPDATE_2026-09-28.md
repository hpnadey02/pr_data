# Office laptop update — 2026-09-28 (SSO login switch)

`.env` mein naya switch: `AUTH_MODE=csv` (abhi jaisa email login, `users.csv` se) ya
`AUTH_MODE=sso` (company SSO — nginx + oauth2-proxy, jaise dusri app mein).

**Pehle zaroori:** `OFFICE_UPDATE_2026-09-25.md` apply ho chuka ho (rbac.py, role wala
login). Neeche ke "FIND" blocks usi ke baad wale code se match karte hain. Koi FIND block na
mile to ruk jao aur file ka woh hissa bhejo — andaaze se paste mat karna.

**Kaise padhein:** "FIND → REPLACE" = FIND wala exact text dhoondo, poora hata ke REPLACE
wala paste karo. "ADD AFTER / ADD BEFORE" = bataayi line ke baad/pehle paste karo.
Indentation (spaces) exactly waise hi rakhna.

## Step 0 — Backup

Office folder ki copy bana lo (`v2_2_backup_2026-09-28`). Backend aur frontend band karo.

## Step 1 — Nayi files: poori copy karo

- `backend/core/sso.py`  ← pehle ye, kyunki auth.py aur streamlit_app.py ise import karte hain
- `deploy/sso/nginx_sso.conf`
- `deploy/sso/oauth2-proxy.cfg.example`
- `scripts/build_oauth2_proxy_config.py`
- `tests/test_sso.py`
- `tests/test_oauth2_proxy_config.py`

## Step 2 — Purani files: blocks badlo

### 2.1 `config/settings.py`

**1. ADD AFTER** line `ALLOWED_LLM_PROVIDERS = ("ollama", "gemini")`:

```python
ALLOWED_AUTH_MODES = ("csv", "sso")
# Anything shorter is guessable, and the secret is all that stops a direct POST to the
# backend from claiming someone else's SSO identity.
SSO_SECRET_MIN_LENGTH = 16
# What scripts/build_oauth2_proxy_config.py needs to write a working oauth2-proxy config.
ENTRA_SETTINGS = ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET",
                  "ENTRA_REDIRECT_URI", "SSO_COOKIE_SECRET")
```

**2. ADD BEFORE** `class Settings` ke andar ki comment line `    # Data files` (jo
`USERS_CSV: str = ...` ke theek upar hai):

```python
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

```

**3. ADD AFTER** poora `def _check_chart_type(...)` function (uski aakhri line
`        return value` ke baad, ek khaali line chhod ke):

```python
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
```

**4. FIND:**

```python
    @field_validator(
        "LOCAL_DATA_PATH", "LOCAL_SHEET_NAME", "GEMINI_API_KEY", mode="before"
    )
```

**REPLACE:**

```python
    @field_validator(
        "LOCAL_DATA_PATH", "LOCAL_SHEET_NAME", "GEMINI_API_KEY", "SSO_SHARED_SECRET",
        "ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET", "ENTRA_REDIRECT_URI",
        "SSO_COOKIE_SECRET", "SSO_EMAIL_DOMAINS",
        mode="before",
    )
```

**5. FIND** (`_check_active_backend` ka aakhri hissa):

```python
                "LLM_PROVIDER=ollama to run fully locally."
            )
        return self
```

**REPLACE:**

```python
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
```

**6. ADD BEFORE** ye do lines:

```python
    @property
    def odbc_connection_string(self) -> str:
```

naya block:

```python
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

```

### 2.2 `backend/models/schemas.py`

**ADD BEFORE** line `class LoginResponse(BaseModel):`

```python
class SsoLoginRequest(BaseModel):
    # The identity headers the Streamlit server received from nginx, forwarded as they
    # came (backend/core/sso.IDENTITY_HEADERS). The backend decides who they name.
    headers: dict[str, str]


```

### 2.3 `backend/api/auth.py`

**1. FIND** (file ka top — docstring se `router = ...` tak):

```python
"""Allow-list authentication against data/users.csv - issues a session_id used by /chat
for conversational history, per-session question numbering, and idle timeout."""
from fastapi import APIRouter, HTTPException

from backend.core import security
from backend.core.logging_config import get_logger
from backend.models.schemas import LoginRequest, LoginResponse, LogoutRequest
from backend.services import logging_service, user_service

logger = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])
```

**REPLACE:**

```python
"""Login - AUTH_MODE in .env picks the only way in:

  csv -> POST /auth/login with an email listed in data/users.csv.
  sso -> POST /auth/sso from the Streamlit server, carrying the identity headers nginx +
         oauth2-proxy attached and SSO_SHARED_SECRET. /auth/login is refused in this mode,
         or the email form would be a way around the company sign-in.

Either way the session_id issued here is what /chat uses for history, per-session question
numbering and idle timeout, and the role comes from users.csv.
"""
import hmac

from fastapi import APIRouter, Header, HTTPException

from backend.core import security, sso
from backend.core.logging_config import get_logger
from backend.core.rbac import ROLE_USER
from backend.models.schemas import LoginRequest, LoginResponse, LogoutRequest, SsoLoginRequest
from backend.services import logging_service, user_service
from config.settings import get_settings

logger = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

NO_SSO_IDENTITY = (
    "Single sign-on did not say who you are. Open the app through the company SSO address "
    "(nginx), not directly on port 8501. If you already are, oauth2-proxy needs "
    "--set-xauthrequest=true (or --set-authorization-header=true) and nginx must forward "
    "X-Auth-Request-Email - see deploy/sso/nginx_sso.conf."
)


def _open_session(user: dict) -> LoginResponse:
    session = security.create_session(
        user["user_id"], user["user_name"], user["user_email"], role=user.get("role", ROLE_USER),
    )
    return LoginResponse(
        session_id=session["session_id"], user_id=user["user_id"], user_name=user["user_name"],
        role=security.session_role(session),
    )
```

**2. FIND** (poora `login` function):

```python
@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest):
    user = user_service.find_user_by_email(payload.email)
    if not user:
        raise HTTPException(
            status_code=403,
            detail="Access denied. This email is not in the authorized users list - contact your admin.",
        )
    session = security.create_session(
        user["user_id"], user["user_name"], user["user_email"], role=user.get("role", "user"),
    )
    return LoginResponse(
        session_id=session["session_id"], user_id=user["user_id"], user_name=user["user_name"],
        role=security.session_role(session),
    )
```

**REPLACE:**

```python
@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest):
    if settings.is_sso:
        raise HTTPException(
            status_code=403,
            detail="Email login is off: this app signs in through SSO (AUTH_MODE=sso). Open "
                   "it through the company SSO address. If the page still shows the email "
                   "form, restart the frontend so it reads the same .env as the backend.",
        )
    user = user_service.find_user_by_email(payload.email)
    if not user:
        raise HTTPException(
            status_code=403,
            detail="Access denied. This email is not in the authorized users list - contact your admin.",
        )
    return _open_session(user)


@router.post("/sso", response_model=LoginResponse)
def sso_login(payload: SsoLoginRequest, x_sso_secret: str = Header(default="")):
    if not settings.is_sso:
        raise HTTPException(
            status_code=403,
            detail="SSO login is off on the backend (AUTH_MODE=csv). Set AUTH_MODE=sso in "
                   ".env and restart BOTH the backend and the frontend.",
        )
    if not hmac.compare_digest(x_sso_secret.encode(), settings.SSO_SHARED_SECRET.encode()):
        logger.warning("SSO login refused: missing or wrong %s header", sso.SECRET_HEADER)
        raise HTTPException(
            status_code=401,
            detail="SSO login refused: the frontend's SSO_SHARED_SECRET does not match the "
                   "backend's. Both must read the same value from .env - restart both after "
                   "changing it.",
        )

    identity = sso.identity_from_headers(payload.headers)
    if identity is None:
        logger.warning("SSO login refused: no identity in forwarded headers %s",
                       sorted(payload.headers))
        raise HTTPException(status_code=401, detail=NO_SSO_IDENTITY)

    listed = user_service.find_user_by_email(identity.email)
    if listed is None and settings.SSO_REQUIRE_USERS_CSV:
        logger.warning("SSO login refused: %s is not in users.csv (SSO_REQUIRE_USERS_CSV=true)",
                       identity.email)
        raise HTTPException(
            status_code=403,
            detail=f"Access denied. {identity.email} signed in through SSO but is not in the "
                   "authorized users list - contact your admin.",
        )
    if listed is None:
        # The company sign-in is the gate, and least privilege is the role.
        user = {"user_id": identity.email, "user_name": identity.name,
                "user_email": identity.email, "role": ROLE_USER}
    else:
        # The users.csv id and name, so one person's chat-log rows match in both modes.
        user = {**listed, "user_name": listed["user_name"] or identity.name}

    logger.info("SSO login email=%s (from %s) listed_in_users_csv=%s",
                identity.email, identity.source, listed is not None)
    return _open_session(user)
```

`logout` function jaisa hai waisa rehne do.

### 2.4 `backend/api/health.py`

**ADD AFTER** line `        "chromadb": {"ok": chroma_ok, "detail": chroma_msg},`

```python
        # Never the secret itself - only whether one is set.
        "auth": {
            "mode": settings.AUTH_MODE,
            "detail": settings.describe_auth(),
            "users_csv_found": settings.resolved(settings.USERS_CSV).exists(),
            "sso_secret_set": bool(settings.SSO_SHARED_SECRET),
            # The ENTRA_* values only feed oauth2-proxy's config; listed so a missing one
            # is visible here, by name.
            "entra_tenant_id": settings.ENTRA_TENANT_ID,
            "entra_client_id": settings.ENTRA_CLIENT_ID,
            "entra_redirect_uri": settings.ENTRA_REDIRECT_URI,
            "entra_missing": settings.entra_missing(),
        },
```

### 2.5 `backend/main.py`

**ADD AFTER** line `    logger.info("Active LLM provider: %s", settings.describe_llm())`

```python
    logger.info("Active login: %s", settings.describe_auth())
```

### 2.6 `frontend/streamlit_app.py`

**1. ADD BEFORE** line `from backend.core.figure_codec import figure_from_json`

```python
from backend.core import sso
```

**2. FIND:**

```python
def _api(path: str, json_body: dict, timeout: float | None = None) -> tuple[bool, dict | str]:
    url = f"{settings.BACKEND_URL}{path}"
    try:
        resp = httpx.post(url, json=json_body, timeout=timeout or settings.FRONTEND_REQUEST_TIMEOUT_SECONDS)
```

**REPLACE:**

```python
def _api(
    path: str, json_body: dict, timeout: float | None = None, headers: dict | None = None,
) -> tuple[bool, dict | str]:
    url = f"{settings.BACKEND_URL}{path}"
    try:
        resp = httpx.post(
            url, json=json_body, timeout=timeout or settings.FRONTEND_REQUEST_TIMEOUT_SECONDS,
            headers=headers,
        )
```

**3. FIND** (poora `_login_screen` function):

```python
def _login_screen():
    form_col, mascot_col = st.columns([3, 2], gap="large", vertical_alignment="center")
    with mascot_col:
        render_mascot()
    with form_col:
        _login_form()
```

**REPLACE:**

```python
def _start_session(login_response: dict) -> None:
    st.session_state.logged_in = True
    st.session_state.session_id = login_response["session_id"]
    st.session_state.user_id = login_response["user_id"]
    st.session_state.user_name = login_response["user_name"]
    st.session_state.role = _role_from(login_response)


def _proxy_identity_headers() -> dict[str, str]:
    try:
        headers = st.context.headers
    except RuntimeError:  # no Streamlit server behind this run (AppTest, `python app.py`)
        return {}
    return sso.forwarded_identity_headers(headers)


def _sso_sign_in() -> str | None:
    """Signs in whoever nginx + oauth2-proxy vouch for; returns why not when it cannot."""
    headers = _proxy_identity_headers()
    if not headers:
        return (
            "Single sign-on is on (AUTH_MODE=sso) but this page was opened without a "
            "signed-in identity. Open it through the company SSO address (nginx), not "
            "directly on port 8501."
        )
    ok, data = _api(
        "/auth/sso", {"headers": headers}, timeout=15,
        headers={sso.SECRET_HEADER: settings.SSO_SHARED_SECRET},
    )
    if not ok:
        return data
    _start_session(data)
    return None


def _login_screen(sso_error: str | None):
    form_col, mascot_col = st.columns([3, 2], gap="large", vertical_alignment="center")
    with mascot_col:
        render_mascot()
    with form_col:
        if settings.is_sso:
            # Only reached when the automatic sign-in failed - there is no form to fall back
            # to, or it would be a way around the company sign-in.
            st.markdown("### 🔐 Single sign-on")
            st.error(sso_error or "Single sign-on failed.")
            st.caption("Refresh the page once this is fixed.")
        else:
            _login_form()
```

**4. FIND** (`_login_form` ke andar):

```python
        if ok:
            st.session_state.logged_in = True
            st.session_state.session_id = data["session_id"]
            st.session_state.user_id = data["user_id"]
            st.session_state.user_name = data["user_name"]
            st.session_state.role = _role_from(data)
            st.rerun()
```

**REPLACE:**

```python
        if ok:
            _start_session(data)
            st.rerun()
```

**5. FIND** (`main()` ka shuru):

```python
def main():
    _init_state()
    apply_theme()
    with st.container(key="uni-top"):
```

**REPLACE:**

```python
def main():
    _init_state()
    apply_theme()
    sso_error = None
    if settings.is_sso and not st.session_state.logged_in:
        # No form in this mode: the sign-in happens on the page's first run, before the
        # header needs the user's name. A refresh after an idle-out signs in again.
        sso_error = _sso_sign_in()
    with st.container(key="uni-top"):
```

**6. FIND** (`main()` ke andar):

```python
    if not st.session_state.logged_in:
        _login_screen()
```

**REPLACE:**

```python
    if not st.session_state.logged_in:
        _login_screen(sso_error)
```

## Step 3 — Ye files poori copy kar sakte ho (runtime par asar nahi)

- `tests/test_rbac.py`
- `tests/test_frontend_render.py`
- `.env.example`
- `.gitignore`  ← generated `deploy/sso/oauth2-proxy.cfg` (Entra secret) ko git se bahar rakhta hai
- `CLAUDE.md`
- `README.md`

## Step 4 — `.env` (office) — ye lines jodo

Office laptop Windows par chal raha hai, wahan nginx nahi hai — isliye abhi `csv` hi rakho.
Secret office laptop par hi naya banao (personal wala copy mat karna):

```bat
.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```ini
# ======================= LOGIN SWITCH =============================
AUTH_MODE=csv
SSO_SHARED_SECRET=<upar wale command ka output>
SSO_REQUIRE_USERS_CSV=false

# ---------------- Microsoft Entra ID (sso only) ----------------
# Directory (tenant) ID - GUID, app registration ka Overview page
ENTRA_TENANT_ID=
# Application (client) ID - GUID, wahi page
ENTRA_CLIENT_ID=
# Certificates & secrets -> secret ka VALUE column (Secret ID nahi)
ENTRA_CLIENT_SECRET=
# https://<chatbot-host>/oauth2/callback - Entra ke Authentication mein exactly yahi
ENTRA_REDIRECT_URI=
# python -c "import secrets; print(secrets.token_hex(16))"
SSO_COOKIE_SECRET=
# * = tenant ka koi bhi user; ya comma list, jaise universalsompo.com
SSO_EMAIL_DOMAINS=*
```

Entra ki values sirf `.env` mein likhni hain, kisi aur file mein nahi.

## Step 5 — Check karo

```bat
.venv\Scripts\python -m pytest
```

Sirf `test_pyarrow_is_not_installed` fail hona chahiye (purana, known). Backend start log mein
ye line aani chahiye: `Active login: csv -> email login against ./data/users.csv`.
Email login pehle jaisa chalna chahiye.

## Step 6 — SSO chalu karna (Linux server, nginx + oauth2-proxy ke saath)

1. oauth2-proxy: `.env` mein Entra wali values bharo (Step 4), phir
   `python scripts/build_oauth2_proxy_config.py`. Ye values check karta hai aur
   `deploy/sso/oauth2-proxy.cfg` likhta hai. Koi value galat ho to file nahi likhta aur
   har problem naam se batata hai. oauth2-proxy ko `--config=deploy/sso/oauth2-proxy.cfg`
   ke saath chalao. Entra app ke Authentication mein redirect URI exactly `.env` wala hi ho.
2. nginx: `deploy/sso/nginx_sso.conf` (dusri app ke `sso.conf` jaisa hi hai, bas identity
   headers extra forward hote hain). App `/unisonic/` par milegi.
3. Streamlit: `streamlit run frontend/streamlit_app.py --server.baseUrlPath=unisonic --server.address=127.0.0.1`
   (docker mein `0.0.0.0`, par port publish MAT karna).
4. Backend `127.0.0.1` par (ya sirf docker network par). Port 8000 aur 8501 bahar se nahi
   khulne chahiye — warna koi bhi nginx ko bypass karke apna email header bhej sakta hai.
5. Us server ke `.env` mein `AUTH_MODE=sso` + naya `SSO_SHARED_SECRET`. Backend aur frontend
   dono restart.

Check: `http://<host>/` → company login → chatbot khud sign-in karke khulega, header mein
naam + role. `/health/detailed` → `auth.mode` = `sso`.

Ye real nginx + oauth2-proxy + company IdP ke saath abhi tak test nahi hua — jo bhi error
screen ya backend log line aaye (`SSO login refused: ...`), exact text bhejo.
