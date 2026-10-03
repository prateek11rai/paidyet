"""Integration tests run against `uv run poe up`: the real Temporal dev server, the real Ollama, real Telegram.

They never skip. If something they need is missing, they fail and say what to do.
Telegram messages go only to ADMIN_USER_ID, prefixed "[test]".
"""

import uuid

import httpx
import pytest
import sentry_sdk
from dotenv import load_dotenv
from telegram import Bot
from temporalio.client import Client
from temporalio.worker import Worker

from main import SentryClientInterceptor, SentryWorkerInterceptor, sentry_options
from paidyet.activities import Activities
from paidyet.config import TEMPORAL_ADDRESS, Settings, load_settings
from paidyet.store import Store
from paidyet.workflows import ReminderWorkflow

PREFIX = "[test] "


@pytest.fixture(scope="session")
def settings() -> Settings:
    load_dotenv()
    s = load_settings()
    if s.sentry_dsn:
        sentry_sdk.init(**sentry_options(s.sentry_dsn, environment="integration"))
    return s


@pytest.fixture(scope="session")
def admin(settings) -> int:
    missing = [name for name, value in (("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token), ("ADMIN_USER_ID", settings.admin_user_id)) if not value]
    if missing:
        pytest.fail(f"These tests message Telegram and need {' and '.join(missing)} in .env (they message only the admin).")
    return settings.admin_user_id


@pytest.fixture(scope="session")
async def http(settings):
    async with httpx.AsyncClient(base_url=settings.ollama_url, timeout=180) as client:
        try:
            tags = (await client.get("/api/tags")).json()
        except httpx.HTTPError:
            pytest.fail(f"Ollama isn't reachable at {settings.ollama_url}. Run `uv run poe up` first.")
        if not any(m["name"] == settings.ollama_model for m in tags.get("models", [])):
            pytest.fail(f"{settings.ollama_model} isn't pulled. Run `uv run poe up` first (it pulls the model).")
        yield client


@pytest.fixture(scope="session")
async def temporal(settings) -> Client:
    try:
        return await Client.connect(TEMPORAL_ADDRESS, interceptors=[SentryClientInterceptor()])
    except RuntimeError:
        pytest.fail(f"Temporal isn't reachable at {TEMPORAL_ADDRESS}. Run `uv run poe up` first.")


@pytest.fixture(scope="session")
async def telegram(settings, admin):
    async with Bot(settings.telegram_bot_token) as bot:
        yield bot


@pytest.fixture(scope="session")
def store(settings):
    s = Store(settings.data_dir / "integration.db")  # never the real paidyet.db
    yield s
    s.close()


@pytest.fixture(scope="session")
async def worker(settings, temporal, http, telegram, store):
    """Our own worker on our own task queue, so tests never touch the app's reminders."""
    queue = f"paidyet-it-{uuid.uuid4().hex[:8]}"
    activities = Activities(settings, store, http, telegram, only_chat=settings.admin_user_id, prefix=PREFIX)
    async with Worker(
        temporal, task_queue=queue, workflows=[ReminderWorkflow], activities=activities.all(),
        interceptors=[SentryWorkerInterceptor()],
    ) as w:  # fmt: skip
        yield w
    sentry_sdk.flush()
