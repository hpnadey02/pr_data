"""Who the SSO proxy says is signed in (AUTH_MODE=sso).

The app never sees a password in this mode. nginx asks oauth2-proxy about every request
(auth_request) and forwards the answer as request headers; this module reads them.

They are trusted only because nginx OVERWRITES each of them on every request
(proxy_set_header), so whatever a browser sends under those names never arrives. That holds
only while Streamlit is reachable through nginx alone - see deploy/sso/nginx_sso.conf.

The ID token's signature is not re-checked here: oauth2-proxy verified it before nginx let
the request through. Nor is its expiry - oauth2-proxy keeps forwarding the original ID token
for as long as its own session cookie is valid, so an expiry check would lock people out an
hour after they signed in.
"""
import base64
import json
import re
from dataclasses import dataclass
from typing import Mapping, Optional

# Sent by the Streamlit server with the identity; the backend refuses an identity without it.
SECRET_HEADER = "X-SSO-Secret"

EMAIL_HEADER = "x-auth-request-email"
USER_HEADER = "x-auth-request-user"
PREFERRED_USERNAME_HEADER = "x-auth-request-preferred-username"
AUTHORIZATION_HEADER = "authorization"
# Exactly the headers nginx_sso.conf sets. Nothing else is forwarded or read: nginx passes
# every other browser header through unchanged, so e.g. X-Forwarded-Email could be typed
# by anyone.
IDENTITY_HEADERS = (EMAIL_HEADER, USER_HEADER, PREFERRED_USERNAME_HEADER, AUTHORIZATION_HEADER)

# Azure AD carries the sign-in address in preferred_username (upn / unique_name on v1
# tokens) unless the optional "email" claim is configured; Keycloak and Google send email.
_EMAIL_CLAIMS = ("email", "preferred_username", "upn", "unique_name")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class SsoIdentity:
    email: str
    name: str
    # Which header or token claim supplied the email - logged, so a wrong identity can be
    # traced to the proxy setting that produced it.
    source: str


def forwarded_identity_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """The identity headers present in `headers`, keyed lower-case."""
    picked: dict[str, str] = {}
    for key, value in headers.items():
        name = str(key).strip().lower()
        text = str(value or "").strip()
        if name in IDENTITY_HEADERS and text:
            picked[name] = text
    return picked


def token_claims(authorization: str) -> dict:
    """The payload of an `Authorization: Bearer <JWT>` value; {} for anything else."""
    scheme, _, token = str(authorization or "").strip().partition(" ")
    parts = token.strip().split(".")
    if scheme.lower() != "bearer" or len(parts) != 3:
        return {}
    payload = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:  # bad base64, bad UTF-8 and bad JSON all land here
        return {}
    return claims if isinstance(claims, dict) else {}


def _as_email(value) -> str:
    text = str(value or "").strip().lower()
    return text if _EMAIL_RE.match(text) else ""


def _display_name(claims: dict, found: dict[str, str], email: str) -> str:
    name = str(claims.get("name") or "").strip()
    if not name:
        name = " ".join(
            str(claims.get(key) or "").strip() for key in ("given_name", "family_name")
        ).strip()
    if not name:
        username = found.get(PREFERRED_USERNAME_HEADER, "")
        name = "" if "@" in username else username
    return name or email.split("@")[0]


def identity_from_headers(headers: Mapping[str, str]) -> Optional[SsoIdentity]:
    """The signed-in person, or None when the headers name nobody.

    oauth2-proxy's own X-Auth-Request-Email wins. The ID token comes next - it is all the
    plain reference nginx config forwards. The username headers are last, and count only
    when they hold an email: for OIDC, X-Auth-Request-User is usually the opaque subject id.
    """
    found = forwarded_identity_headers(headers)
    claims = token_claims(found.get(AUTHORIZATION_HEADER, ""))

    candidates = [(EMAIL_HEADER, found.get(EMAIL_HEADER))]
    candidates += [(f"token claim '{claim}'", claims.get(claim)) for claim in _EMAIL_CLAIMS]
    candidates += [(header, found.get(header)) for header in (PREFERRED_USERNAME_HEADER, USER_HEADER)]
    for source, value in candidates:
        email = _as_email(value)
        if email:
            return SsoIdentity(email=email, name=_display_name(claims, found, email), source=source)
    return None
