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


@router.post("/logout")
def logout(payload: LogoutRequest):
    session = security.close_session(payload.session_id)
    if session:
        logging_service.update_logout_time(session["user_id"], session["login_time"].isoformat())
    return {"status": "ok"}
