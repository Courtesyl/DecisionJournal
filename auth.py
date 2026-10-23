import hashlib
import json
import secrets
from pathlib import Path

import db

AUTH_PATH = db.DB_PATH.parent / "auth.json"

_MIN_PASSWORD_LEN = 4
_PBKDF2_ITERATIONS = 200_000


def _pbkdf2(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)


def load_auth() -> dict | None:
    try:
        data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and "hash" in data and "salt" in data else None
    except (OSError, ValueError):
        return None


def save_password(password: str) -> None:
    password = password.strip()
    if len(password) < _MIN_PASSWORD_LEN:
        raise ValueError(f"пароль минимум {_MIN_PASSWORD_LEN} символа")
    salt = secrets.token_bytes(16)
    AUTH_PATH.write_text(
        json.dumps({
            "salt": salt.hex(),
            "hash": _pbkdf2(password, salt).hex(),
            "session_secret": secrets.token_hex(32),
        }),
        encoding="utf-8",
    )


def verify_password(password: str) -> bool:
    data = load_auth()
    if not data:
        return False
    calculated = _pbkdf2(password.strip(), bytes.fromhex(data["salt"])).hex()
    return secrets.compare_digest(calculated, data["hash"])


def session_secret() -> str:
    data = load_auth()
    if data and data.get("session_secret"):
        return data["session_secret"]
    return secrets.token_hex(32)
