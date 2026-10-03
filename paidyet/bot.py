"""Telegram side: allowlist gate, capture, buttons, snooze calendar, and how messages read."""

import calendar
import functools
import logging
import re
import secrets
from datetime import date, datetime, timedelta

import sentry_sdk
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
from paidyet.store import Row, Store
from paidyet.workflows import MORNING, ConfirmView, ReminderInput, ReminderView, ReminderWorkflow, Status

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


def plan_phrase(plan: list[str], demo_every_s: int | None, now: datetime, tz) -> str:
    """The whole plan: "Tue 6 Oct 10 AM, Thu 8 Oct 7 PM, Fri 9 Oct 10 AM and 7 PM, then every morning until it's paid"."""
    times = [datetime.fromisoformat(t).astimezone(tz) for t in plan]
    then = "then every morning until it's paid"
    if not times:
        return "every morning until it's paid"
    if demo_every_s:
        every = max(1, round(demo_every_s / 60))
        return f"{remind_phrase(times[0], now)}, then every {every} min ({len(times)} times in all), {then}"
    by_day: dict[date, list[str]] = {}
    for t in times:
        by_day.setdefault(t.date(), []).append(clock(t))
    parts = []
    for d, clocks in by_day.items():
        label = "today" if d == now.date() else "tomorrow" if d == now.date() + timedelta(days=1) else day(d)
        parts.append(f"{label} {' and '.join(clocks)}")
    return f"{', '.join(parts)}, {then}"


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
    if view.plan:
        lines.append(f"I'll remind {owner} {plan_phrase(view.plan, view.demo_every_s, now, settings.tz)}.")
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
        return (
            f"{settings.name_of(view.added_by)} added a reminder for you:\n{head} · due {when(due, d.has_time)}\n"
            f"I'll remind you {plan_phrase(view.plan, view.demo_every_s, now, settings.tz)}."
        )
    status = due_status(due, d.has_time, now)
    text = f"⏰ {head}\n{status[0].upper()}{status[1:]} · {who}"
    if view.reason == "last":
        text += "\nThat's a month overdue, so I'll stop reminding you. It stays in /due until you tap Paid."
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


def reminder_keyboard(rid: str, reason: str = "reminder") -> InlineKeyboardMarkup:
    """The "added for you" notice and the last message only offer Paid: there's no reminder there to snooze."""
    paid = _button("✅ Paid", f"paid:{rid}")
    if reason in ("added", "last"):
        return InlineKeyboardMarkup([[paid]])
    return InlineKeyboardMarkup([[paid, _button("😴 Snooze", f"snz:{rid}")]])


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


def traced(name: str):
    """Each Telegram update is a Sentry transaction; the workflow it starts continues the same trace."""

    def wrap(handler):
        @functools.wraps(handler)
        async def inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            with sentry_sdk.start_transaction(op="telegram.update", name=name):
                await handler(update, context)

        return inner

    return wrap


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
    user, settings = update.effective_user, _settings(context)
    text = f"{USAGE}\n\nYour Telegram ID is {user.id}."
    if settings.is_admin(user.id):
        text += (
            "\n\nAs the admin you can add reminders for friends: /remind arjun ₹500 to Rahul by Fri, "
            "or send a bill photo captioned \"for arjun\"."
        )
    await update.effective_message.reply_text(text)


def due_lines(rows: list[Row], viewer: int, settings: Settings, now: datetime) -> tuple[list[str], list[str]]:
    overdue, upcoming = [], []
    for r in rows:
        due = datetime.fromisoformat(r.due_at).astimezone(settings.tz)
        late = due < now if r.has_time else due.date() < now.date()
        payee = f" to {r.payee}" if r.payee else ""
        line = f"• {r.title} · {inr(r.amount_inr)}{payee} · {due_status(due, r.has_time, now)} · {added_by(r.added_by, viewer, settings)}"
        (overdue if late else upcoming).append(line)
    return overdue, upcoming


def due_message(viewer: int, settings: Settings, store: Store, now: datetime, skip: str | None = None):
    mine = [r for r in store.open_for(viewer) if r.id != skip]
    overdue, upcoming = due_lines(mine, viewer, settings, now)
    parts = []
    if overdue:
        parts.append("🔴 Overdue\n" + "\n".join(overdue))
    if upcoming:
        parts.append("🗓 Upcoming\n" + "\n".join(upcoming))
    if settings.is_admin(viewer):
        others = [r for r in store.open_added_by(viewer) if r.id != skip]
        if others:
            lines = [f"• {settings.name_of(r.owner_id)}: {r.title} · {inr(r.amount_inr)} · "
                     f"{due_status(datetime.fromisoformat(r.due_at).astimezone(settings.tz), r.has_time, now)}"
                     for r in others]  # fmt: skip
            parts.append("👀 You added for others\n" + "\n".join(lines))
    if not parts:
        return "Nothing due. 🎉", None
    buttons = [[_button(f"✅ Paid: {r.title}"[:40], f"dpaid:{r.id}")] for r in mine[:8]]
    return "\n\n".join(parts), InlineKeyboardMarkup(buttons) if buttons else None


@traced("telegram /due")
async def due(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    text, markup = due_message(update.effective_user.id, settings, context.bot_data["store"], datetime.now(settings.tz))
    await update.effective_message.reply_text(text, reply_markup=markup)


@traced("telegram /remind")
async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/remind <name> <what>: the admin adds a reminder for a friend."""
    msg, user, settings = update.effective_message, update.effective_user, _settings(context)
    if not settings.is_admin(user.id):
        await msg.reply_text("Only the admin can add reminders for other people. Just send me your own bill or note.")
        return
    friends = ", ".join(n for uid, n in settings.allowed_users.items() if uid != user.id) or "nobody yet"
    if len(context.args) < 2:
        await msg.reply_text(f"Usage: /remind <name> <what>, e.g. /remind arjun ₹500 to Rahul by Fri\nFriends: {friends}")
        return
    target = settings.user_id_for(context.args[0])
    if target is None or target == user.id:
        await msg.reply_text(f"I don't know {context.args[0]!r}. Friends: {friends}")
        return
    await _start_reminder(context, msg, owner_id=target, added_by=user.id, text=" ".join(context.args[1:]))


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
    with sentry_sdk.start_span(op="telegram.download", name="download photo"):
        data = await (await media.get_file()).download_as_bytearray()
        return extract.save_photo(settings.tmp_dir, bytes(data), suffix)


@traced("telegram capture")
async def capture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg, user, settings = update.effective_message, update.effective_user, _settings(context)

    reply_to = msg.reply_to_message.message_id if msg.reply_to_message else None
    if pending := take_pending(context.user_data, reply_to, bool(msg.text)):
        if pending["kind"] == "fix":
            await _signal(context, pending["rid"], "fix", extract.remember_text(msg.text))
            await _react(msg)
        else:
            await _snooze_to_typed_date(context, msg, pending["rid"])
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

    owner = user.id
    # The admin can caption a bill photo "for arjun" to add it to a friend's list.
    if photo_name and msg.caption and settings.is_admin(user.id) and (m := FOR_NAME.match(msg.caption)):
        target = settings.user_id_for(m["name"])
        if target is None:
            extract.discard_photo(settings.tmp_dir, photo_name)
            await msg.reply_text(f"I don't know {m['name']!r}. Check ALLOWED_USERS.")
            return
        owner, text = target, m["rest"] or None
    await _start_reminder(context, msg, owner_id=owner, added_by=user.id, text=text, photo_name=photo_name)


def take_pending(user_data: dict, reply_to: int | None, has_text: bool) -> dict | None:
    """The Fix or date prompt this message answers. Only a reply to that prompt counts: any other message
    cancels the prompt and is read as a new bill or note."""
    pending = user_data.pop("awaiting", None)
    if pending and has_text and reply_to == pending["prompt_id"]:
        return pending
    return None


async def _ask(context: ContextTypes.DEFAULT_TYPE, message: Message, kind: str, rid: str, text: str) -> None:
    """Send a prompt the user answers by replying to it (ForceReply makes their message a reply)."""
    prompt = await message.reply_text(f"{text}\n/cancel to stop.", reply_markup=ForceReply(selective=True))
    context.user_data["awaiting"] = {"kind": kind, "rid": rid, "prompt_id": prompt.message_id}


async def _snooze_to_typed_date(context: ContextTypes.DEFAULT_TYPE, msg: Message, rid: str) -> None:
    settings = _settings(context)
    now = datetime.now(settings.tz)
    picked = extract.parse_due(msg.text, extract.printed_date(msg.text, now), now)
    if picked is None or picked[0].date() <= now.date():
        await _ask(context, msg, "date", rid, "I didn't get that date. Reply with something like 12 Oct.")
        return
    until = datetime.combine(picked[0].date(), MORNING, tzinfo=settings.tz)
    try:
        status = await _temporal(context).get_workflow_handle(rid).query(ReminderWorkflow.status)
    except RPCError:
        await msg.reply_text("I can't find that reminder any more.")
        return
    await msg.reply_text(await _snooze(context, rid, until, status))


async def _snooze(context: ContextTypes.DEFAULT_TYPE, rid: str, until: datetime, status: Status) -> str:
    """Snooze only postpones: a time before the next scheduled reminder is refused, not applied."""
    tz = _settings(context).tz
    if status.state == "parked":
        return "This one only lives in /due now. Tap Paid when it's done."
    if status.state != "scheduled":
        return "This one isn't waiting on a reminder."
    if status.next_at and until <= (next_at := datetime.fromisoformat(status.next_at).astimezone(tz)):
        return f"That's before your next reminder ({day(next_at)}, {clock(next_at)})."
    await _signal(context, rid, "snooze", until.isoformat())
    return f"Snoozed until {day(until)}, {clock(until)}"


@traced("telegram /cancel")
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    had = context.user_data.pop("awaiting", None)
    await update.effective_message.reply_text("Okay, cancelled." if had else "Nothing to cancel.")


FOR_NAME = re.compile(r"^\s*for\s+(?P<name>[\w.-]+)\s*[:,-]?\s*(?P<rest>.*)$", re.I | re.S)


async def _start_reminder(
    context: ContextTypes.DEFAULT_TYPE, msg: Message, *, owner_id: int, added_by: int,
    text: str | None, photo_name: str | None = None,
) -> None:  # fmt: skip
    settings = _settings(context)
    status = await msg.reply_text("👀 Reading it…")
    text_key = extract.remember_text(text) if text else None
    inp = ReminderInput(
        owner_id=owner_id,
        added_by=added_by,
        origin_chat_id=msg.chat_id,
        origin_message_id=msg.message_id,
        status_message_id=status.message_id,
        sent_at=msg.date.astimezone(settings.tz).isoformat(),
        photo_name=photo_name,
        text_key=text_key,
    )
    try:
        with sentry_sdk.start_span(op="temporal.start_workflow", name="start ReminderWorkflow"):
            await _temporal(context).start_workflow(ReminderWorkflow.run, inp, id=new_reminder_id(), task_queue=TASK_QUEUE)
    except RPCError:
        log.exception("could not start a reminder workflow")
        if photo_name:
            extract.discard_photo(settings.tmp_dir, photo_name)
        if text_key:
            extract.forget_text(text_key)
        await status.edit_text("I can't reach my scheduler right now. Try again in a minute.")


@traced("telegram button")
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
            await q.answer()
            await _ask(context, q.message, "fix", rid,
                       "What should I change? Reply to this message, e.g. amount is 1340, or due 12 Oct.")  # fmt: skip
        case "save" | "fix":
            await q.answer("This one is already saved.")
        case "paid":
            await _signal(context, rid, "paid")
            await q.answer("Marked as paid ✅")
        case "dpaid":  # from the /due list: refresh the list without it
            await _signal(context, rid, "paid")
            await q.answer("Marked as paid ✅")
            text, markup = due_message(user.id, settings, context.bot_data["store"], now, skip=rid)
            try:
                await q.edit_message_text(text, reply_markup=markup)
            except TelegramError:
                pass
        case "snz":
            await _edit_markup(q, snooze_keyboard(rid))
            await q.answer()
        case "back":
            await _edit_markup(q, reminder_keyboard(rid))
            await q.answer()
        case "snz1" | "snz3":
            until = datetime.combine(now.date() + timedelta(days=1 if action == "snz1" else 3), MORNING, tzinfo=settings.tz)
            await q.answer(await _snooze(context, rid, until, status), show_alert=False)
        case "pick":
            await _edit_markup(q, calendar_keyboard(rid, now.year, now.month, now.date()))
            await q.answer()
            await _ask(context, q.message, "date", rid, "Tap a day above, or reply to this message with a date like 12 Oct.")
        case "cal" if re.fullmatch(r"\d{6}", arg):
            await _edit_markup(q, calendar_keyboard(rid, int(arg[:4]), int(arg[4:]), now.date()))
            await q.answer()
        case "day" if re.fullmatch(r"\d{8}", arg):
            picked = datetime.strptime(arg, "%Y%m%d").date()
            if picked <= now.date():
                await q.answer("Pick a day after today.")
                return
            if (context.user_data.get("awaiting") or {}).get("rid") == rid:
                context.user_data.pop("awaiting")
            until = datetime.combine(picked, MORNING, tzinfo=settings.tz)
            await q.answer(await _snooze(context, rid, until, status))
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


def build(settings: Settings, temporal: Client, store: Store) -> Application:
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .connect_timeout(10)
        .read_timeout(15)
        .write_timeout(15)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["temporal"] = temporal
    app.bot_data["store"] = store
    private = filters.ChatType.PRIVATE
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(CommandHandler("start", start, filters=private))
    app.add_handler(CommandHandler("due", due, filters=private))
    app.add_handler(CommandHandler("remind", remind, filters=private))
    app.add_handler(CommandHandler("cancel", cancel, filters=private))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(
        MessageHandler(private & (filters.TEXT & ~filters.COMMAND | filters.PHOTO | filters.Document.ALL), capture)
    )
    return app
