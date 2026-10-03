"""PaidYet entry point: loads .env, starts Sentry, runs the Temporal worker and the Telegram bot together."""

import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram.error import InvalidToken
from temporalio.client import Client

from paidyet import bot, extract
from paidyet.config import TEMPORAL_ADDRESS, ConfigError, Settings, load_settings

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


def init_sentry(settings: Settings) -> None:
    if not settings.sentry_dsn:
        log.info("SENTRY_DSN is not set: running without Sentry")
        return
    import sentry_sdk
    from sentry_sdk.integrations.httpx import HttpxIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=1.0,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        # Its spans carry request URLs, and Telegram's contain the bot token.
        disabled_integrations=[HttpxIntegration()],
    )
    log.info("Sentry: enabled (no PII, no prompts, no local variables)")


async def run(settings: Settings) -> None:
    try:
        client = await Client.connect(TEMPORAL_ADDRESS)
    except RuntimeError as e:
        raise SystemExit(f"Temporal isn't reachable at {TEMPORAL_ADDRESS}. Start everything with `uv run poe up`.") from e
    log.info("Temporal: connected to %s (namespace %s)", TEMPORAL_ADDRESS, client.namespace)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with contextlib.AsyncExitStack() as stack:
        if settings.telegram_bot_token:
            app = bot.build(settings)
            try:
                await stack.enter_async_context(app)
            except InvalidToken as e:
                raise SystemExit("TELEGRAM_BOT_TOKEN was rejected by Telegram; check it in .env") from e
            await app.start()
            stack.push_async_callback(app.stop)
            await app.updater.start_polling()
            stack.push_async_callback(app.updater.stop)
            log.info("Telegram: polling as @%s", app.bot.username)
        else:
            log.info("TELEGRAM_BOT_TOKEN is not set: skipping the Telegram bot")

        log.info("PaidYet is up. Ctrl-C to stop.")
        await stop.wait()
        log.info("shutting down")


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
