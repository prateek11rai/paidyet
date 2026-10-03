"""ReminderWorkflow: read -> confirm -> remind until Paid. Timers, signals and a query; no I/O in this module.

Activities are called by name so their I/O libraries never enter Temporal's workflow sandbox.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.worker import (
    ExecuteWorkflowInput,
    StartActivityInput,
    WorkflowInboundInterceptor,
    WorkflowOutboundInterceptor,
)

with workflow.unsafe.imports_passed_through():
    from paidyet.extract import Draft

EVENING = time(19, 0)  # the reminder the day before
MORNING = time(10, 0)  # due-day and overdue nudges, snoozes
CONFIRM_WINDOW = timedelta(days=7)
MIN_NUDGE = timedelta(minutes=1)
DAY = timedelta(days=1)

READ_ATTEMPTS = 3
READ = dict(
    start_to_close_timeout=timedelta(minutes=3),  # a cold model load alone takes ~25 s
    retry_policy=RetryPolicy(
        initial_interval=timedelta(seconds=2), maximum_attempts=READ_ATTEMPTS, non_retryable_error_types=["InputGone"]
    ),
)
QUICK = dict(
    start_to_close_timeout=timedelta(seconds=30),
    # Telegram may be unreachable while the laptop is offline: keep trying, slowly.
    retry_policy=RetryPolicy(maximum_interval=timedelta(minutes=5), non_retryable_error_types=["Forbidden"]),
)


# --- Payloads. Only IDs, keys and validated fields: never images or raw text. ---------------------


@dataclass
class ReminderInput:
    owner_id: int  # who owes the money and gets the reminders
    added_by: int  # the owner, or the admin who added it for them
    origin_chat_id: int  # the chat the bill or message was sent in
    origin_message_id: int
    status_message_id: int  # the "Reading it…" reply, later edited into the confirm message
    sent_at: str  # ISO; relative dates ("Friday tak") are read from this moment
    photo_name: str | None = None
    text_key: str | None = None


@dataclass
class ReadRequest:
    sent_at: str
    photo_name: str | None = None
    text_key: str | None = None


@dataclass
class FixRequest:
    previous: Draft
    correction_key: str


@dataclass
class ConfirmView:
    reminder_id: str
    chat_id: int
    message_id: int
    draft: Draft
    owner_id: int
    added_by: int
    remind_at: str | None
    state: str  # "draft" | "saved" | "expired"


@dataclass
class ReminderView:
    reminder_id: str
    chat_id: int
    draft: Draft
    owner_id: int
    added_by: int
    reason: str  # "added" (the admin added it for you) | "reminder" | "snoozed"
    now: str
    next_at: str | None = None
    previous_message_id: int | None = None  # strip its buttons, or edit it when snoozing


@dataclass
class SavedReminder:
    reminder_id: str
    owner_id: int
    added_by: int
    draft: Draft
    next_at: str


@dataclass
class PaidUpdate:
    reminder_id: str
    owner_id: int
    added_by: int
    draft: Draft
    paid_at: str
    message_id: int | None


@dataclass
class Notice:
    chat_id: int
    text: str
    reply_to: int | None = None
    edit_message_id: int | None = None


@dataclass
class Status:
    state: str  # reading | confirming | fixing | scheduled | paid | failed | expired
    owner_id: int
    added_by: int
    draft: Draft | None = None
    next_at: str | None = None
    reminders_sent: int = 0
    paid_at: str | None = None
    message_id: int | None = None


# --- Schedule: pure functions of the due date and "now" -----------------------------------------


def _at(d, t: time, tz) -> datetime:
    return datetime.combine(d, t, tzinfo=tz)


def first_reminder(due: datetime, has_time: bool, now: datetime) -> datetime:
    """Date dues: 7 PM the day before, else 10 AM or 7 PM on the day, else in a minute. Timed dues: at that time."""
    if has_time:
        return max(due, now)
    local = now.astimezone(due.tzinfo)
    for candidate in (_at(due.date() - DAY, EVENING, due.tzinfo), _at(due.date(), MORNING, due.tzinfo), _at(due.date(), EVENING, due.tzinfo)):
        if candidate > local:
            return candidate
    return local + MIN_NUDGE


def nudge_interval(due: datetime, has_time: bool, sent_at: datetime) -> timedelta:
    """Daily, except a short demo due ("in 2 minutes") repeats at its own pace, from 1 minute up to a day."""
    if not has_time:
        return DAY
    return min(max(due - sent_at, MIN_NUDGE), DAY)


def next_nudge(due: datetime, has_time: bool, fired_at: datetime, every: timedelta) -> datetime:
    """After a reminder: 10 AM on the due day, then 10 AM every day until Paid."""
    if has_time:
        return fired_at + every
    local = fired_at.astimezone(due.tzinfo)
    return _at(max(due.date(), local.date() + DAY), MORNING, due.tzinfo)


# --- The workflow ----------------------------------------------------------------------------------


@workflow.defn
class ReminderWorkflow:
    def __init__(self) -> None:
        self._inp: ReminderInput | None = None
        self._state = "reading"
        self._draft: Draft | None = None
        self._actions: list[tuple[str, str | None]] = []
        self._paid = False
        self._snooze_until: datetime | None = None
        self._next_at: datetime | None = None
        self._sent = 0
        self._paid_at: datetime | None = None
        self._message_id: int | None = None

    # Signals: the buttons. The bot checks who tapped before it signals.

    @workflow.signal
    def save(self) -> None:
        self._actions.append(("save", None))

    @workflow.signal
    def fix(self, correction_key: str) -> None:
        self._actions.append(("fix", correction_key))

    @workflow.signal
    def paid(self) -> None:
        self._paid = True

    @workflow.signal
    def snooze(self, until: str) -> None:
        self._snooze_until = datetime.fromisoformat(until)

    @workflow.query
    def status(self) -> Status:
        inp = self._inp
        return Status(
            state=self._state,
            owner_id=inp.owner_id if inp else 0,
            added_by=inp.added_by if inp else 0,
            draft=self._draft,
            next_at=self._next_at.isoformat() if self._next_at else None,
            reminders_sent=self._sent,
            paid_at=self._paid_at.isoformat() if self._paid_at else None,
            message_id=self._message_id,
        )

    @workflow.run
    async def run(self, inp: ReminderInput) -> str:
        self._inp = inp
        if not await self._read():
            return self._state
        if not await self._confirm():
            return self._state
        await self._remind()
        return self._state

    async def _read(self) -> bool:
        inp = self._inp
        request = ReadRequest(sent_at=inp.sent_at, photo_name=inp.photo_name, text_key=inp.text_key)
        try:
            self._draft = await workflow.execute_activity("read_input", request, result_type=Draft, **READ)
        except ActivityError:
            self._state = "failed"
        finally:
            # Success or final failure: the photo and the text are gone from here on.
            await workflow.execute_activity("discard_input", request, **QUICK)
        if self._state == "failed":
            await self._notice(
                "Sorry, I couldn't read that. Send it again, or type it like: Rahul ko 500 dene hai Friday tak",
                edit=inp.status_message_id,
            )
            return False
        return True

    async def _confirm(self) -> bool:
        self._state = "confirming"
        await self._show_confirm("draft")
        while True:
            try:
                await workflow.wait_condition(lambda: bool(self._actions), timeout=CONFIRM_WINDOW)
            except asyncio.TimeoutError:
                self._state = "expired"
                await self._show_confirm("expired")
                return False
            action, arg = self._actions.pop(0)
            if action == "save" and self._draft.ready:
                return True
            if action == "fix":
                self._state = "fixing"
                try:
                    self._draft = await workflow.execute_activity(
                        "fix_draft", FixRequest(previous=self._draft, correction_key=arg), result_type=Draft, **READ
                    )
                except ActivityError:
                    await self._notice("I couldn't apply that fix. Tap Fix and try again.", reply=self._inp.status_message_id)
                self._state = "confirming"
                await self._show_confirm("draft")

    async def _remind(self) -> None:
        inp, draft = self._inp, self._draft
        due = datetime.fromisoformat(draft.due_at)
        every = nudge_interval(due, draft.has_time, datetime.fromisoformat(inp.sent_at))
        self._state = "scheduled"
        self._next_at = first_reminder(due, draft.has_time, workflow.now())
        await workflow.execute_activity(
            "save_reminder",
            SavedReminder(self._rid(), inp.owner_id, inp.added_by, draft, self._next_at.isoformat()),
            **QUICK,
        )
        await self._show_confirm("saved")
        if inp.added_by != inp.owner_id:
            await self._show_reminder("added")

        applied = None
        while not self._paid:
            if self._snooze_until is not None and self._snooze_until != applied:
                applied = self._snooze_until
                if applied > workflow.now():
                    self._next_at = applied
                    await self._store_next()
                    await self._show_reminder("snoozed", edit=True)
            wait = (self._next_at - workflow.now()).total_seconds()
            if wait > 0:
                try:
                    await workflow.wait_condition(
                        lambda: self._paid or self._snooze_until != applied, timeout=timedelta(seconds=wait)
                    )
                    continue  # Paid or a new snooze: handled at the top of the loop
                except asyncio.TimeoutError:
                    pass
            self._sent += 1
            await self._show_reminder("reminder")
            self._next_at = next_nudge(due, draft.has_time, workflow.now(), every)
            await self._store_next()

        self._state = "paid"
        self._paid_at = workflow.now()
        self._next_at = None
        await workflow.execute_activity(
            "mark_paid",
            PaidUpdate(self._rid(), inp.owner_id, inp.added_by, draft, self._paid_at.isoformat(), self._message_id),
            **QUICK,
        )

    # Helpers that turn state into activity calls.

    def _rid(self) -> str:
        return workflow.info().workflow_id

    async def _store_next(self) -> None:
        inp = self._inp
        saved = SavedReminder(self._rid(), inp.owner_id, inp.added_by, self._draft, self._next_at.isoformat())
        await workflow.execute_activity("set_next", saved, **QUICK)

    async def _show_confirm(self, state: str) -> None:
        inp = self._inp
        remind_at = None
        if self._draft.ready and state != "expired":
            due = datetime.fromisoformat(self._draft.due_at)
            remind_at = (self._next_at or first_reminder(due, self._draft.has_time, workflow.now())).isoformat()
        view = ConfirmView(self._rid(), inp.origin_chat_id, inp.status_message_id, self._draft, inp.owner_id, inp.added_by, remind_at, state)
        await workflow.execute_activity("show_confirm", view, **QUICK)

    async def _show_reminder(self, reason: str, edit: bool = False) -> None:
        inp = self._inp
        view = ReminderView(
            reminder_id=self._rid(),
            chat_id=inp.owner_id,
            draft=self._draft,
            owner_id=inp.owner_id,
            added_by=inp.added_by,
            reason=reason,
            now=workflow.now().isoformat(),
            next_at=self._next_at.isoformat() if self._next_at else None,
            previous_message_id=self._message_id,
        )
        name = "edit_reminder" if edit and self._message_id else "send_reminder"
        try:
            message_id = await workflow.execute_activity(name, view, result_type=int, **QUICK)
        except ActivityError:
            # Telegram refuses to message someone who never opened the bot (or blocked it). Keep the
            # reminder alive and tell whoever added it; the next nudge tries again.
            if reason == "added":
                await self._notice(
                    "I couldn't message them on Telegram. Ask them to open the bot and send /start; "
                    "I'll keep trying at each reminder."
                )
            return
        self._message_id = message_id or self._message_id

    async def _notice(self, text: str, edit: int | None = None, reply: int | None = None) -> None:
        notice = Notice(chat_id=self._inp.origin_chat_id, text=text, reply_to=reply, edit_message_id=edit)
        await workflow.execute_activity("notify", notice, **QUICK)


# --- Tracing: carry the Sentry trace from workflow start to every activity ----------------------

TRACE_HEADERS = ("sentry-trace", "baggage")


class TraceHeaders(WorkflowInboundInterceptor):
    """Copies the trace headers the bot set on workflow start onto each activity. Opaque copies, no I/O."""

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        self.trace: dict = {}
        super().init(_TraceOutbound(outbound, self))

    async def execute_workflow(self, input: ExecuteWorkflowInput):
        self.trace = {k: v for k, v in input.headers.items() if k in TRACE_HEADERS}
        return await super().execute_workflow(input)


class _TraceOutbound(WorkflowOutboundInterceptor):
    def __init__(self, next: WorkflowOutboundInterceptor, inbound: TraceHeaders) -> None:
        super().__init__(next)
        self._inbound = inbound

    def start_activity(self, input: StartActivityInput):
        input.headers = {**input.headers, **self._inbound.trace}
        return super().start_activity(input)
