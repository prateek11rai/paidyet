"""Side effects for ReminderWorkflow: Gemma, Telegram and SQLite. Logs carry IDs and timings, never content."""

import logging
from datetime import datetime

import httpx
from telegram import Bot, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden
from temporalio import activity
from temporalio.exceptions import ApplicationError

from paidyet import bot as ui
from paidyet import extract
from paidyet.config import Settings
from paidyet.extract import Draft
from paidyet.store import Store
from paidyet.workflows import (
    READ_ATTEMPTS,
    ConfirmView,
    FixRequest,
    Notice,
    PaidUpdate,
    ReadRequest,
    ReminderView,
    SavedReminder,
)

log = logging.getLogger(__name__)


def _gone(what: str) -> ApplicationError:
    # Inputs don't survive a restart (photos are swept, texts live in memory): ask the user to resend.
    return ApplicationError(f"{what} is gone", type="InputGone", non_retryable=True)


class Activities:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        http: httpx.AsyncClient,
        telegram: Bot | None,
        *,
        only_chat: int | None = None,
        prefix: str = "",
    ):
        self.settings = settings
        self.store = store
        self.http = http
        self.telegram = telegram
        self.only_chat = only_chat  # integration tests send everything to the admin
        self.prefix = prefix

    def all(self) -> list:
        return [
            self.read_input,
            self.discard_input,
            self.fix_draft,
            self.show_confirm,
            self.send_reminder,
            self.edit_reminder,
            self.save_reminder,
            self.set_next,
            self.mark_paid,
            self.notify,
        ]

    # --- Gemma ---

    @activity.defn(name="read_input")
    async def read_input(self, req: ReadRequest) -> Draft:
        image = None
        if req.photo_name:
            path = extract.photo_path(self.settings.tmp_dir, req.photo_name)
            if not path.exists():
                raise _gone("the photo")
            image = path.read_bytes()
        text = None
        if req.text_key:
            text = extract.recall_text(req.text_key)
            if text is None:
                raise _gone("the message")
        sent_at = datetime.fromisoformat(req.sent_at).astimezone(self.settings.tz)
        draft, usages = await extract.read(self.http, self.settings.ollama_model, sent_at, text=text, image=image)
        self._log_usage("read", usages, draft)
        return draft

    @activity.defn(name="discard_input")
    async def discard_input(self, req: ReadRequest) -> None:
        if req.photo_name:
            extract.discard_photo(self.settings.tmp_dir, req.photo_name)
        if req.text_key:
            extract.forget_text(req.text_key)

    @activity.defn(name="fix_draft")
    async def fix_draft(self, req: FixRequest) -> Draft:
        correction = extract.recall_text(req.correction_key)
        if correction is None:
            raise _gone("the correction")
        now = datetime.now(self.settings.tz)
        try:
            draft, usages = await extract.read(
                self.http, self.settings.ollama_model, now, previous=req.previous, correction=correction
            )
        except Exception:
            if activity.info().attempt >= READ_ATTEMPTS:
                extract.forget_text(req.correction_key)
            raise
        extract.forget_text(req.correction_key)
        self._log_usage("fix", usages, draft)
        return draft

    def _log_usage(self, what: str, usages: list[extract.Usage], draft: Draft) -> None:
        log.info(
            "%s: %d call(s), %d in / %d out tokens, %d ms (load %d ms), ready=%s",
            what,
            len(usages),
            sum(u.input_tokens for u in usages),
            sum(u.output_tokens for u in usages),
            sum(u.total_ms for u in usages),
            sum(u.load_ms for u in usages),
            draft.ready,
        )

    # --- Telegram ---

    def _bot(self) -> Bot:
        if self.telegram is None:
            # Retryable: once a token is set and PaidYet restarts, waiting messages go out.
            raise ApplicationError("TELEGRAM_BOT_TOKEN is not set", type="NoBot")
        return self.telegram

    def _chat(self, chat_id: int) -> int:
        return self.only_chat or chat_id

    async def _send(self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None, reply_to: int | None = None) -> int:
        try:
            msg = await self._bot().send_message(
                self._chat(chat_id), self.prefix + text, reply_markup=markup,
                reply_to_message_id=None if self.only_chat else reply_to, allow_sending_without_reply=True,
            )  # fmt: skip
        except Forbidden as e:
            raise ApplicationError("the user blocked the bot", type="Forbidden", non_retryable=True) from e
        return msg.message_id

    async def _edit(self, chat_id: int, message_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
        try:
            await self._bot().edit_message_text(self.prefix + text, self._chat(chat_id), message_id, reply_markup=markup)
            return True
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return True
            if "not found" in str(e).lower() or "can't be edited" in str(e).lower():
                return False
            raise
        except Forbidden as e:
            raise ApplicationError("the user blocked the bot", type="Forbidden", non_retryable=True) from e

    async def _strip_buttons(self, chat_id: int, message_id: int) -> None:
        try:
            await self._bot().edit_message_reply_markup(self._chat(chat_id), message_id, reply_markup=None)
        except BadRequest:
            pass

    @activity.defn(name="show_confirm")
    async def show_confirm(self, view: ConfirmView) -> None:
        now = datetime.now(self.settings.tz)
        text = ui.confirm_text(view, self.settings, now)
        markup = ui.confirm_keyboard(view.reminder_id, view.draft) if view.state == "draft" else None
        if not await self._edit(view.chat_id, view.message_id, text, markup):
            await self._send(view.chat_id, text, markup)

    @activity.defn(name="send_reminder")
    async def send_reminder(self, view: ReminderView) -> int:
        if view.previous_message_id:
            await self._strip_buttons(view.chat_id, view.previous_message_id)
        text = ui.reminder_text(view, self.settings)
        return await self._send(view.chat_id, text, ui.reminder_keyboard(view.reminder_id))

    @activity.defn(name="edit_reminder")
    async def edit_reminder(self, view: ReminderView) -> int:
        text = ui.reminder_text(view, self.settings)
        markup = ui.reminder_keyboard(view.reminder_id)
        if view.previous_message_id and await self._edit(view.chat_id, view.previous_message_id, text, markup):
            return view.previous_message_id
        return await self._send(view.chat_id, text, markup)

    @activity.defn(name="notify")
    async def notify(self, notice: Notice) -> None:
        if notice.edit_message_id and await self._edit(notice.chat_id, notice.edit_message_id, notice.text):
            return
        await self._send(notice.chat_id, notice.text, reply_to=notice.reply_to)

    # --- SQLite (and the Paid message) ---

    @activity.defn(name="save_reminder")
    async def save_reminder(self, saved: SavedReminder) -> None:
        d = saved.draft
        self.store.save(saved.reminder_id, saved.owner_id, saved.added_by, d.kind, d.title, d.payee,
                        d.amount_inr, d.due_at, d.has_time, saved.next_at)  # fmt: skip

    @activity.defn(name="set_next")
    async def set_next(self, saved: SavedReminder) -> None:
        self.store.set_next(saved.reminder_id, saved.next_at)

    @activity.defn(name="mark_paid")
    async def mark_paid(self, update: PaidUpdate) -> None:
        self.store.mark_paid(update.reminder_id, update.paid_at)
        paid_at = datetime.fromisoformat(update.paid_at)
        text = ui.paid_text(update.draft, paid_at, self.settings.tz)
        if not (update.message_id and await self._edit(update.owner_id, update.message_id, text)):
            await self._send(update.owner_id, text)
        if update.added_by != update.owner_id:
            who = self.settings.name_of(update.owner_id)
            await self._send(update.added_by, f"✅ {who} paid: {ui.summary(update.draft, self.settings.tz)}")
