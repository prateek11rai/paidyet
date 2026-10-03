"""Telegram handlers: allowlist gate and commands."""

import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)

from paidyet.config import Settings

log = logging.getLogger(__name__)

USAGE = (
    "PaidYet reminds you to pay people back.\n\n"
    "Send me a bill photo, a screenshot or PDF, a forwarded message, or just type something like "
    "\"Rahul ko 500 dene hai Friday tak\". I'll read it, you tap Save, and I'll keep reminding you "
    "until you tap Paid.\n\n"
    "/due lists what's upcoming and overdue."
)


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every other handler. Unknown users get their ID and nothing else."""
    user = update.effective_user
    if user is not None and _settings(context).is_allowed(user.id):
        return
    if user is not None and update.effective_message is not None:
        await update.effective_message.reply_text(
            f"This is a private bot. Your Telegram ID is {user.id}; send it to the owner."
        )
    elif update.callback_query is not None:
        await update.callback_query.answer("This is a private bot.")
    log.info("turned away an update from a user who is not on the allowlist")
    raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.effective_message.reply_text(f"{USAGE}\n\nYour Telegram ID is {user.id}.")


def build(settings: Settings) -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.bot_data["settings"] = settings
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(CommandHandler("start", start))
    return app
