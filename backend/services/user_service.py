"""Allow-list authentication backed by data/users.csv (user_id, user_name, user_email, role).

The role column is optional: a file without it still works and everyone is a "user".
"""
import csv
from typing import Optional

from backend.core.logging_config import get_logger
from backend.core.rbac import normalize_role
from config.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()


def find_user_by_email(email: str) -> Optional[dict]:
    path = settings.resolved(settings.USERS_CSV)
    if not path.exists():
        logger.error("users.csv not found at %s", path)
        return None
    email_norm = email.strip().lower()
    try:
        # utf-8-sig: Excel's "CSV UTF-8" prepends a BOM that would otherwise glue onto
        # the first header, so no "user_id" column is found and every user is locked out.
        # Headers match case/space-insensitively because the role column is typed by hand.
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for raw in csv.DictReader(f):
                row = {
                    k.strip().lower(): (v or "").strip()
                    for k, v in raw.items() if isinstance(k, str)  # None = surplus cells
                }
                if row.get("user_email", "").lower() == email_norm:
                    return {
                        "user_id": row["user_id"],
                        "user_name": row["user_name"],
                        "user_email": row["user_email"],
                        "role": normalize_role(row.get("role")),
                    }
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed reading users.csv: %s", exc)
        return None
    return None
