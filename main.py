"""PaidYet entry point: loads .env, starts Sentry, runs the Temporal worker and the Telegram bot together."""

import asyncio
import contextlib
import logging
import re
import signal
import sys
import time
from pathlib import Path

import httpx
import sentry_sdk
from dotenv import load_dotenv
from sentry_sdk.integrations.httpx import HttpxIntegration
from telegram.error import InvalidToken
from temporalio import activity
from temporalio import client as client_interceptors
from temporalio.client import Client
from temporalio.converter import DataConverter
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
    Worker,
    WorkflowInterceptorClassInput,
)

from paidyet import bot, extract
from paidyet.activities import Activities
from paidyet.config import TASK_QUEUE, TEMPORAL_ADDRESS, ConfigError, Settings, load_settings
from paidyet.store import Store
from paidyet.workflows import TRACE_HEADERS, ReminderWorkflow, TraceHeaders

log = logging.getLogger("paidyet")


def setup_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(logs_dir / "paidyet.log")],
    )
    # httpx logs every request URL at INFO, and Telegram Bot API URLs contain the token.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


# No leading \b: in ".../bot123456:ABC..." there is no word boundary between "bot" and the digits.
TOKEN_LIKE = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}")


def scrub(event, _hint=None):
    """Defence in depth: nothing shaped like a Telegram bot token leaves the laptop."""
    if isinstance(event, str):
        return TOKEN_LIKE.sub("[token]", event)
    if isinstance(event, dict):
        return {k: scrub(v) for k, v in event.items()}
    if isinstance(event, list):
        return [scrub(v) for v in event]
    return event


def sentry_options(dsn: str, environment: str = "local") -> dict:
    return dict(
        dsn=dsn,
        environment=environment,
        traces_sample_rate=1.0,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        # Its spans carry request URLs, and Telegram's contain the bot token.
        disabled_integrations=[HttpxIntegration()],
        before_send=scrub,
        before_send_transaction=scrub,
    )


def init_sentry(settings: Settings) -> None:
    if not settings.sentry_dsn:
        log.info("SENTRY_DSN is not set: running without Sentry")
        return
    sentry_sdk.init(**sentry_options(settings.sentry_dsn))
    log.info("Sentry: enabled (no PII, no prompts, no local variables)")


# --- Temporal interceptors: one Sentry trace from the Telegram update through every activity ------

PAYLOADS = DataConverter.default.payload_converter


class SentryClientInterceptor(client_interceptors.Interceptor):
    def intercept_client(self, next: client_interceptors.OutboundInterceptor) -> client_interceptors.OutboundInterceptor:
        return _SentryClientOutbound(next)


class _SentryClientOutbound(client_interceptors.OutboundInterceptor):
    async def start_workflow(self, input: client_interceptors.StartWorkflowInput):
        trace = {"sentry-trace": sentry_sdk.get_traceparent(), "baggage": sentry_sdk.get_baggage()}
        input.headers = {**input.headers, **{k: PAYLOADS.to_payload(v) for k, v in trace.items() if v}}
        return await super().start_workflow(input)


class SentryWorkerInterceptor(Interceptor):
    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _SentryActivity(next)

    def workflow_interceptor_class(self, input: WorkflowInterceptorClassInput):
        return TraceHeaders


class _SentryActivity(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput):
        info = activity.info()
        trace = {k: PAYLOADS.from_payload(v, str) for k, v in input.headers.items() if k in TRACE_HEADERS}
        txn = sentry_sdk.continue_trace(trace, op="temporal.activity", name=f"activity {info.activity_type}")
        with sentry_sdk.start_transaction(txn) as t:
            t.set_tag("temporal.activity", info.activity_type)
            t.set_tag("temporal.workflow_id", info.workflow_id)
            t.set_data("temporal.attempt", info.attempt)
            try:
                return await super().execute_activity(input)
            except Exception as e:
                t.set_status("internal_error")
                sentry_sdk.capture_exception(e)  # every failed attempt, so flaky retries show up
                raise


async def run(settings: Settings) -> None:
    try:
        client = await Client.connect(TEMPORAL_ADDRESS, interceptors=[SentryClientInterceptor()])
    except RuntimeError as e:
        raise SystemExit(f"Temporal isn't reachable at {TEMPORAL_ADDRESS}. Start everything with `uv run poe up`.") from e
    log.info("Temporal: connected to %s (namespace %s)", TEMPORAL_ADDRESS, client.namespace)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    store = Store(settings.db_path)
    async with contextlib.AsyncExitStack() as stack:
        stack.callback(store.close)
        http = await stack.enter_async_context(httpx.AsyncClient(base_url=settings.ollama_url, timeout=180))
        warm_up = asyncio.create_task(warm_up_model(http, settings.ollama_model))

        app = None
        if settings.telegram_bot_token:
            app = bot.build(settings, client, store)
            try:
                await stack.enter_async_context(app)
            except InvalidToken as e:
                raise SystemExit("TELEGRAM_BOT_TOKEN was rejected by Telegram; check it in .env") from e
        else:
            log.info("TELEGRAM_BOT_TOKEN is not set: skipping the Telegram bot (reminders wait until it is set)")

        activities = Activities(settings, store, http, app.bot if app else None)
        worker = Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[ReminderWorkflow],
            activities=activities.all(),
            interceptors=[SentryWorkerInterceptor()],
        )
        await stack.enter_async_context(worker)
        log.info("Temporal: worker running on task queue %r", TASK_QUEUE)

        if app is not None:
            await app.start()
            stack.push_async_callback(app.stop)
            # Keep retrying the bootstrap: one slow Telegram response mustn't stop the bot (e.g. right after wake).
            await app.updater.start_polling(bootstrap_retries=-1)
            stack.push_async_callback(app.updater.stop)
            log.info("Telegram: polling as @%s", app.bot.username)

        log.info("PaidYet is up. Ctrl-C to stop.")
        await stop.wait()
        log.info("shutting down")
        warm_up.cancel()


async def warm_up_model(http: httpx.AsyncClient, model: str) -> None:
    """Load Gemma now so the first message doesn't wait ~20 s for it."""
    started = time.monotonic()
    try:
        r = await http.post("/api/generate", json={"model": model, "keep_alive": extract.OLLAMA_KEEP_ALIVE})
        r.raise_for_status()
        log.info("Ollama: %s loaded in %.1f s", model, time.monotonic() - started)
    except httpx.HTTPError as e:
        log.warning("Ollama: could not preload %s (%s); the first read will be slower", model, type(e).__name__)


def main() -> None:
    load_dotenv()
    try:
        settings = load_settings()
    except ConfigError as e:
        sys.exit(f"Config error: {e}")
    setup_logging(settings.logs_dir)
    init_sentry(settings)
    if swept := extract.sweep(settings.tmp_dir):
        log.info("deleted %d leftover photo(s) from a previous run", swept)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
