"""Telegram side: allowlist gate, capture, buttons, snooze calendar, and how messages read."""

import calendar
import logging
import re
import secrets
from datetime import date, datetime, timedelta

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from temporalio.client import Client
from temporalio.service import RPCError

from paidyet import extract
from paidyet.config import TASK_QUEUE, Settings
from paidyet.extract import Draft
from paidyet.workflows import MORNING, ConfirmView, ReminderInput, ReminderView, ReminderWorkflow

log = logging.getLogger(__name__)

USAGE = (
    "PaidYet reminds you to pay people back.\n\n"
    "Send me a bill photo or screenshot, forward a message, or just type something like "
    "\"Rahul ko 500 dene hai Friday tak\". I'll read it, you tap Save, and I'll keep reminding you "
    "until you tap Paid.\n\n"
    "/due lists what's upcoming and overdue."
)
REMINDER_ID = re.compile(r"^r-[0-9a-f]{12}$")
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_PHOTO_BYTES = 10 * 1024 * 1024


# --- How things read ---------------------------------------------------------------------------


def inr(amount: float | None) -> str:
    """Indian digit grouping: ₹1,240 · ₹15,500 · ₹1,00,000 · ₹99.50"""
    if amount is None:
        return "₹?"
    whole, paise = f"{amount:.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return "₹" + ",".join(groups + [tail]) + ("" if paise == "00" else f".{paise}")


def day(dt: datetime) -> str:
    return f"{dt:%a} {dt.day} {dt:%b}"


def clock(dt: datetime) -> str:
    return f"{dt:%-I %p}" if dt.minute == 0 else f"{dt:%-I:%M %p}"


def when(dt: datetime, has_time: bool) -> str:
    return f"{day(dt)}, {clock(dt)}" if has_time else day(dt)


def remind_phrase(at: datetime, now: datetime) -> str:
    if at - now < timedelta(hours=1):
        minutes = max(1, round((at - now).total_seconds() / 60))
        return f"in {minutes} min" if minutes > 1 else "in a minute"
    return f"{day(at)}, {clock(at)}" if at.date() != now.date() else f"today, {clock(at)}"


def due_status(due: datetime, has_time: bool, now: datetime) -> str:
    if has_time:
        late = now - due
        return "due now" if late < timedelta(minutes=1) else f"overdue by {_span(late)}"
    days = (due.date() - now.date()).days
    if days > 1:
        return f"due {day(due)}, in {days} days"
    if days == 1:
        return f"due tomorrow, {day(due)}"
    if days == 0:
        return "due today"
    return f"overdue by {-days} day{'s' if days < -1 else ''} (was due {day(due)})"


def _span(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 24 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // (24 * 60)} days"


def added_by(added: int, viewer: int, settings: Settings) -> str:
    return "added by you" if added == viewer else f"added by {settings.name_of(added)}"


def summary(draft: Draft, tz) -> str:
    due = when(datetime.fromisoformat(draft.due_at).astimezone(tz), draft.has_time) if draft.due_at else "?"
    payee = f" to {draft.payee}" if draft.payee else ""
    return f"{draft.title} · {inr(draft.amount_inr)}{payee} · due {due}"


def confirm_text(view: ConfirmView, settings: Settings, now: datetime) -> str:
    d, viewer = view.draft, view.added_by
    lines = []
    if view.state == "saved":
        lines.append(f"✅ Saved: {summary(d, settings.tz)}")
    elif view.state == "expired":
        lines.append(f"Not saved: {summary(d, settings.tz)}\nSend it again if you still need a reminder.")
        return "\n".join(lines)
    else:
        lines.append(summary(d, settings.tz))
    owner = "you" if view.owner_id == viewer else settings.name_of(view.owner_id)
    lines.append(f"For {owner} · {added_by(view.added_by, viewer, settings)}")
    if view.remind_at:
        at = datetime.fromisoformat(view.remind_at).astimezone(settings.tz)
        lines.append(f"I'll remind {owner} {remind_phrase(at, now)}.")
    if view.state == "draft":
        lines += [f"⚠️ {p}" for p in d.problems] + [f"ℹ️ {n}" for n in d.notes]
        if d.problems:
            lines.append("Tap Fix and tell me what's right.")
    return "\n".join(lines)


def reminder_text(view: ReminderView, settings: Settings) -> str:
    d = view.draft
    now = datetime.fromisoformat(view.now).astimezone(settings.tz)
    due = datetime.fromisoformat(d.due_at).astimezone(settings.tz)
    payee = f" to {d.payee}" if d.payee else ""
    head = f"{d.title} · {inr(d.amount_inr)}{payee}"
    who = added_by(view.added_by, view.owner_id, settings)
    if view.reason == "added":
        at = datetime.fromisoformat(view.next_at).astimezone(settings.tz)
        return (
            f"{settings.name_of(view.added_by)} added a reminder for you:\n{head} · due {when(due, d.has_time)}\n"
            f"I'll remind you {remind_phrase(at, now)}."
        )
    status = due_status(due, d.has_time, now)
    text = f"⏰ {head}\n{status[0].upper()}{status[1:]} · {who}"
    if view.reason == "snoozed" and view.next_at:
        at = datetime.fromisoformat(view.next_at).astimezone(settings.tz)
        text += f"\n😴 Snoozed until {remind_phrase(at, now)}."
    return text


def paid_text(draft: Draft, paid_at: datetime, tz) -> str:
    return f"✅ Paid on {day(paid_at.astimezone(tz))}: {summary(draft, tz)}"


# --- Keyboards ---------------------------------------------------------------------------------


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def confirm_keyboard(rid: str, draft: Draft) -> InlineKeyboardMarkup:
    row = [_button("✅ Save", f"save:{rid}")] if draft.ready else []
    return InlineKeyboardMarkup([row + [_button("✏️ Fix", f"fix:{rid}")]])


def reminder_keyboard(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_button("✅ Paid", f"paid:{rid}"), _button("😴 Snooze", f"snz:{rid}")]])


def snooze_keyboard(rid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [_button("Tomorrow", f"snz1:{rid}"), _button("In 3 days", f"snz3:{rid}")],
            [_button("📅 Pick date", f"pick:{rid}")],
            [_button("« Back", f"back:{rid}")],
        ]
    )


def calendar_keyboard(rid: str, year: int, month: int, today: date) -> InlineKeyboardMarkup:
    first = date(year, month, 1)
    prev_month = (first - timedelta(days=1)).replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    nav = [
        _button("‹", f"cal:{rid}:{prev_month:%Y%m}") if prev_month >= today.replace(day=1) else _button(" ", "nop"),
        _button(f"{first:%b %Y}", "nop"),
        _button("›", f"cal:{rid}:{next_month:%Y%m}"),
    ]
    rows = [nav, [_button(d, "nop") for d in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")]]
    for week in calendar.Calendar().monthdatescalendar(year, month):
        rows.append(
            [
                _button(str(d.day), f"day:{rid}:{d:%Y%m%d}") if d.month == month and d > today else _button(" ", "nop")
                for d in week
            ]
        )
    rows.append([_button("« Back", f"snz:{rid}")])
    return InlineKeyboardMarkup(rows)


# --- Handlers ----------------------------------------------------------------------------------


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


def _temporal(context: ContextTypes.DEFAULT_TYPE) -> Client:
    return context.bot_data["temporal"]


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every other handler. Unknown users get their ID and nothing else: no model call."""
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


def new_reminder_id() -> str:
    return f"r-{secrets.token_hex(6)}"


def _forwarded_from(msg: Message) -> str | None:
    origin = msg.forward_origin
    if origin is None:
        return None
    for attr in ("sender_user", "sender_chat", "chat"):
        if (who := getattr(origin, attr, None)) is not None:
            return getattr(who, "first_name", None) or getattr(who, "title", None)
    return getattr(origin, "sender_user_name", None)


async def _download_photo(msg: Message, settings: Settings) -> str | None:
    """Save an attached image under .data/tmp and return its random name. None if there isn't one."""
    if msg.photo:
        media, suffix = msg.photo[-1], ".jpg"
    elif msg.document and msg.document.mime_type in IMAGE_TYPES:
        media, suffix = msg.document, IMAGE_TYPES[msg.document.mime_type]
    else:
        return None
    if (media.file_size or 0) > MAX_PHOTO_BYTES:
        raise ValueError("too big")
    data = await (await media.get_file()).download_as_bytearray()
    return extract.save_photo(settings.tmp_dir, bytes(data), suffix)


async def capture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, user, settings = update.effective_message, update.effective_user, _settings(context)

    if msg.text and (rid := context.user_data.pop("fixing", None)):
        await _signal(context, rid, "fix", extract.remember_text(msg.text))
        await _react(msg)
        return
    if msg.text and (rid := context.user_data.pop("snoozing", None)):
        now = datetime.now(settings.tz)
        picked = extract.parse_due(msg.text, extract.printed_date(msg.text, now), now)
        if picked is None or picked[0].date() <= now.date():
            context.user_data["snoozing"] = rid
            await msg.reply_text("I didn't get that date. Try something like 12 Oct, or pick a day above.")
            return
        until = datetime.combine(picked[0].date(), MORNING, tzinfo=settings.tz)
        await _signal(context, rid, "snooze", until.isoformat())
        await _react(msg)
        return

    if msg.document and msg.document.mime_type == "application/pdf":
        await msg.reply_text("I can't read PDFs yet. Send a screenshot of the bill instead.")
        return
    text = msg.text or msg.caption
    try:
        photo_name = await _download_photo(msg, settings)
    except ValueError:
        await msg.reply_text("That image is too big. Send a smaller screenshot.")
        return
    if photo_name is None and not text:
        await msg.reply_text(USAGE)
        return
    if text and (sender := _forwarded_from(msg)):
        text = f"Forwarded from {sender}: {text}"

    status = await msg.reply_text("👀 Reading it…")
    text_key = extract.remember_text(text) if text else None
    inp = ReminderInput(
        owner_id=user.id,
        added_by=user.id,
        origin_chat_id=msg.chat_id,
        origin_message_id=msg.message_id,
        status_message_id=status.message_id,
        sent_at=msg.date.astimezone(settings.tz).isoformat(),
        photo_name=photo_name,
        text_key=text_key,
    )
    try:
        await _temporal(context).start_workflow(ReminderWorkflow.run, inp, id=new_reminder_id(), task_queue=TASK_QUEUE)
    except RPCError:
        log.exception("could not start a reminder workflow")
        if photo_name:
            extract.discard_photo(settings.tmp_dir, photo_name)
        if text_key:
            extract.forget_text(text_key)
        await status.edit_text("I can't reach my scheduler right now. Try again in a minute.")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q, user, settings = update.callback_query, update.effective_user, _settings(context)
    action, _, rest = (q.data or "").partition(":")
    rid, _, arg = rest.partition(":")
    if action == "nop" or not REMINDER_ID.match(rid):
        await q.answer()
        return
    try:
        status = await _temporal(context).get_workflow_handle(rid).query(ReminderWorkflow.status)
    except RPCError:
        await q.answer("I can't find that reminder any more.")
        return
    # Save and Fix belong to whoever added it; Paid and Snooze to whoever owes.
    allowed = status.added_by if action in ("save", "fix") else status.owner_id
    if user.id != allowed:
        await q.answer("That one isn't yours to change.")
        return
    if status.state == "paid":
        await q.answer("Already paid. ✅")
        return

    now = datetime.now(settings.tz)
    match action:
        case "save" if status.state == "confirming":
            await _signal(context, rid, "save")
            await q.answer("Saved")
        case "fix" if status.state in ("confirming", "fixing"):
            context.user_data["fixing"] = rid
            await q.answer()
            await q.message.reply_text(
                "What should I change? For example: amount is 1340, or due 12 Oct.",
                reply_markup=ForceReply(selective=True),
            )
        case "save" | "fix":
            await q.answer("This one is already saved.")
        case "paid":
            await _signal(context, rid, "paid")
            await q.answer("Marked as paid ✅")
        case "snz":
            context.user_data.pop("snoozing", None)
            await _edit_markup(q, snooze_keyboard(rid))
            await q.answer()
        case "back":
            await _edit_markup(q, reminder_keyboard(rid))
            await q.answer()
        case "snz1" | "snz3":
            until = datetime.combine(now.date() + timedelta(days=1 if action == "snz1" else 3), MORNING, tzinfo=settings.tz)
            await _signal(context, rid, "snooze", until.isoformat())
            await q.answer(f"Snoozed until {day(until)}, {clock(until)}")
        case "pick":
            context.user_data["snoozing"] = rid
            await _edit_markup(q, calendar_keyboard(rid, now.year, now.month, now.date()))
            await q.answer("Pick a day, or type a date like 12 Oct")
        case "cal" if re.fullmatch(r"\d{6}", arg):
            await _edit_markup(q, calendar_keyboard(rid, int(arg[:4]), int(arg[4:]), now.date()))
            await q.answer()
        case "day" if re.fullmatch(r"\d{8}", arg):
            picked = datetime.strptime(arg, "%Y%m%d").date()
            if picked <= now.date():
                await q.answer("Pick a day after today.")
                return
            context.user_data.pop("snoozing", None)
            until = datetime.combine(picked, MORNING, tzinfo=settings.tz)
            await _signal(context, rid, "snooze", until.isoformat())
            await q.answer(f"Snoozed until {day(until)}, {clock(until)}")
        case _:
            await q.answer()


async def _signal(context: ContextTypes.DEFAULT_TYPE, rid: str, name: str, arg: str | None = None) -> None:
    handle = _temporal(context).get_workflow_handle(rid)
    method = getattr(ReminderWorkflow, name)
    await (handle.signal(method, arg) if arg is not None else handle.signal(method))


async def _edit_markup(q, markup: InlineKeyboardMarkup) -> None:
    try:
        await q.edit_message_reply_markup(markup)
    except TelegramError:
        pass  # "message is not modified" and friends


async def _react(msg: Message) -> None:
    try:
        await msg.set_reaction("👌")
    except TelegramError:
        pass


def build(settings: Settings, temporal: Client) -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.bot_data["settings"] = settings
    app.bot_data["temporal"] = temporal
    private = filters.ChatType.PRIVATE
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(CommandHandler("start", start, filters=private))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(
        MessageHandler(private & (filters.TEXT & ~filters.COMMAND | filters.PHOTO | filters.Document.ALL), capture)
    )
    return app
