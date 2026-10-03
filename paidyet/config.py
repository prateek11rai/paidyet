"""Typed settings read from the environment (main.py loads .env first)."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent
TEMPORAL_ADDRESS = "127.0.0.1:7233"
TASK_QUEUE = "paidyet"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str | None = field(repr=False)
    sentry_dsn: str | None = field(repr=False)
    admin_user_id: int | None
    allowed_users: dict[int, str]  # Telegram user ID -> display name
    ollama_model: str
    ollama_host: str
    data_dir: Path
    tz: ZoneInfo

    @property
    def ollama_url(self) -> str:
        return f"http://{self.ollama_host}"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "paidyet.db"

    def is_allowed(self, user_id: int) -> bool:
        return user_id in self.allowed_users or user_id == self.admin_user_id

    def is_admin(self, user_id: int) -> bool:
        return self.admin_user_id is not None and user_id == self.admin_user_id

    def name_of(self, user_id: int) -> str:
        return self.allowed_users.get(user_id, "the admin" if self.is_admin(user_id) else "someone")

    def user_id_for(self, name: str) -> int | None:
        wanted = name.strip().lower()
        return next((uid for uid, n in self.allowed_users.items() if n.lower() == wanted), None)


def parse_allowed_users(raw: str) -> dict[int, str]:
    """`"me:111,arjun:222"` -> `{111: "me", 222: "arjun"}`."""
    users: dict[int, str] = {}
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        name, sep, uid = pair.rpartition(":")
        if not sep or not name.strip() or not uid.strip().isdigit():
            raise ConfigError(f"ALLOWED_USERS entries must look like name:telegram_id, got {pair!r}")
        users[int(uid)] = name.strip()
    return users


def _optional(env: Mapping[str, str], key: str) -> str | None:
    return env.get(key, "").strip() or None


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    admin = _optional(env, "ADMIN_USER_ID")
    if admin is not None and not admin.isdigit():
        raise ConfigError("ADMIN_USER_ID must be a numeric Telegram user ID")

    ollama_host = _optional(env, "OLLAMA_HOST") or "127.0.0.1:11434"
    if ollama_host.rpartition(":")[0] not in LOOPBACK_HOSTS:
        raise ConfigError(f"OLLAMA_HOST must be a loopback address like 127.0.0.1:11434, got {ollama_host}")

    tz_name = _optional(env, "TZ") or "Asia/Kolkata"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(f"TZ {tz_name!r} is not a known time zone") from e

    data_dir = Path(_optional(env, "PAIDYET_DATA_DIR") or ".data")
    return Settings(
        telegram_bot_token=_optional(env, "TELEGRAM_BOT_TOKEN"),
        sentry_dsn=_optional(env, "SENTRY_DSN"),
        admin_user_id=int(admin) if admin else None,
        allowed_users=parse_allowed_users(env.get("ALLOWED_USERS", "")),
        ollama_model=_optional(env, "OLLAMA_MODEL") or "gemma4:e4b",
        ollama_host=ollama_host,
        data_dir=data_dir if data_dir.is_absolute() else ROOT / data_dir,
        tz=tz,
    )
