"""ReminderWorkflow: read -> confirm -> remind until Paid. Timers, signals and a query; no I/O in this module.

Activities are called by name so their I/O libraries never enter Temporal's workflow sandbox.
"""

import asyncio
from dataclasses import dataclass, field
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

# The schedule (see `plan` and `next_reminder`).
MORNING = time(10, 0)  # early notice, due day, overdue nudges, snoozes
EVENING = time(19, 0)  # the evening before, and the due day's last call
QUIET_START, QUIET_END = time(22, 0), time(8, 0)
QUIET_EARLIER = time(21, 30)  # where a pre-deadline reminder goes instead of into quiet hours
EARLY_NOTICE = timedelta(days=3)
LEAD = timedelta(minutes=30)  # timed dues at least LEAD_FROM away get a reminder this much before
LEAD_FROM = timedelta(hours=1)
DEMO_WINDOW = timedelta(minutes=10)  # dues closer than this repeat at their own pace (demos)
DEMO_REPEATS = 5
MIN_NUDGE = timedelta(minutes=1)
OVERDUE_CAP = timedelta(days=30)  # then one last message, and the reminder lives on in /due only
DAY = timedelta(days=1)

CONFIRM_WINDOW = timedelta(days=7)
CONTINUE_AFTER = 50  # reminders per run before continue-as-new keeps the history short

READ_ATTEMPTS = 3
READ = dict(
    start_to_close_timeout=timedelta(minutes=3),  # a cold model load alone takes ~25 s
    retry_policy=RetryPolicy(
        initial_interval=timedelta(seconds=2), maximum_attempts=READ_ATTEMPTS, non_retryable_error_types=["InputGone"]
    ),
)
TELEGRAM = dict(
    start_to_close_timeout=timedelta(seconds=30),
    # Telegram may be unreachable while the laptop is offline: keep trying, slowly. A refusal is final.
    retry_policy=RetryPolicy(maximum_interval=timedelta(minutes=5), non_retryable_error_types=["Forbidden", "BadRequest"]),
)
STORE = dict(
    start_to_close_timeout=timedelta(seconds=10),
    # SQLite and local files: if they fail five times, it's a bug, not weather. Fail and say so.
    retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=30), maximum_attempts=5),
)


# --- Payloads. Only IDs, keys and validated fields: never images or raw text. ---------------------


@dataclass
class Carry:
    """State handed to the next run by continue-as-new."""

    draft: Draft
    set_at: str
    saved_at: str
    next_at: str | None
    sent: int
    message_id: int | None
    snooze_until: str | None
    snooze_applied: str | None  # a snooze can arrive just before the hand-over, not yet applied


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
    carry: Carry | None = None


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
    state: str  # "draft" | "saved" | "expired"
    plan: list[str] = field(default_factory=list)  # upcoming reminders, before the daily overdue ones
    demo_every_s: int | None = None


@dataclass
class ReminderView:
    reminder_id: str
    chat_id: int
    draft: Draft
    owner_id: int
    added_by: int
    reason: str  # "added" | "reminder" | "snoozed" | "last"
    now: str
    next_at: str | None = None
    previous_message_id: int | None = None  # strip its buttons, or edit it when snoozing
    plan: list[str] = field(default_factory=list)
    demo_every_s: int | None = None


@dataclass
class SavedReminder:
    reminder_id: str
    owner_id: int
    added_by: int
    draft: Draft
    next_at: str | None


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
    state: str  # reading | confirming | fixing | scheduled | parked | paid | failed | expired
    owner_id: int
    added_by: int
    draft: Draft | None = None
    next_at: str | None = None
    reminders_sent: int = 0
    paid_at: str | None = None
    message_id: int | None = None


# --- Schedule: pure functions of the due date and when it was set ---------------------------------


def _at(d, t: time, tz) -> datetime:
    return datetime.combine(d, t, tzinfo=tz)


def _quiet(when: datetime, before_deadline: bool) -> datetime:
    """Keep reminders out of 22:00–08:00: earlier (21:30) if it must beat the deadline, else later (08:00)."""
    t, tz = when.timetz().replace(tzinfo=None), when.tzinfo
    if QUIET_END <= t < QUIET_START:
        return when
    if before_deadline:
        return _at(when.date() if t >= QUIET_START else when.date() - DAY, QUIET_EARLIER, tz)
    return _at(when.date() + DAY if t >= QUIET_START else when.date(), QUIET_END, tz)


def demo_every(due: datetime, has_time: bool, set_at: datetime) -> timedelta | None:
    """A due set less than 10 minutes ahead is a demo: it repeats at that pace, a few times."""
    lead = due - set_at
    return max(lead, MIN_NUDGE) if has_time and lead < DEMO_WINDOW else None


def plan(due: datetime, has_time: bool, set_at: datetime, saved_at: datetime) -> list[datetime]:
    """The reminders before the daily overdue nudges begin. `set_at` is when the due was read or fixed."""
    tz = due.tzinfo
    if not has_time:
        d = due.date()
        early = [_at(d - EARLY_NOTICE, MORNING, tz)] if d - saved_at.astimezone(tz).date() >= EARLY_NOTICE else []
        return early + [_at(d - DAY, EVENING, tz), _at(d, MORNING, tz), _at(d, EVENING, tz)]
    if every := demo_every(due, has_time, set_at):
        return [due + every * k for k in range(DEMO_REPEATS)]
    slots = {_quiet(due, before_deadline=False)}
    if due - set_at >= LEAD_FROM:
        slots.add(_quiet(due - LEAD, before_deadline=True))
    return sorted(slots)


def next_reminder(due: datetime, has_time: bool, slots: list[datetime], after: datetime) -> datetime | None:
    """The first reminder strictly after `after`: a planned one, else 10 AM daily while overdue, for 30 days."""
    for slot in slots:
        if slot > after:
            return slot
    tz = due.tzinfo
    candidate = _at(after.astimezone(tz).date(), MORNING, tz)
    if candidate <= after:
        candidate += DAY
    if not has_time:
        candidate = max(candidate, _at(due.date() + DAY, MORNING, tz))
    return candidate if candidate <= due + OVERDUE_CAP else None


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
        self._set_at: datetime | None = None
        self._saved_at: datetime | None = None
        self._applied: datetime | None = None  # the last snooze the loop acted on

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
        if inp.carry is None:
            if not await self._read():
                return self._state
            if not await self._confirm():
                return self._state
            await self._save()
        else:
            self._resume(inp.carry)
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
            await self._store("discard_input", request)
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

    async def _save(self) -> None:
        inp = self._inp
        self._set_at = self._draft_set_at()
        self._saved_at = workflow.now()
        self._next_at = next_reminder(self._due(), self._draft.has_time, self._slots(), self._saved_at)
        self._state = "scheduled"
        if not await self._store("save_reminder", self._saved()):
            await self._notice("Saved, but I couldn't add it to /due. The reminders still work.")
        await self._show_confirm("saved")
        if inp.added_by != inp.owner_id:
            await self._show_reminder("added")

    def _resume(self, carry: Carry) -> None:
        self._draft = carry.draft
        self._set_at = datetime.fromisoformat(carry.set_at)
        self._saved_at = datetime.fromisoformat(carry.saved_at)
        self._next_at = datetime.fromisoformat(carry.next_at) if carry.next_at else None
        self._sent = carry.sent
        self._message_id = carry.message_id
        self._snooze_until = datetime.fromisoformat(carry.snooze_until) if carry.snooze_until else None
        self._applied = datetime.fromisoformat(carry.snooze_applied) if carry.snooze_applied else None
        self._state = "scheduled" if self._next_at else "parked"

    async def _remind(self) -> None:
        due, has_time, slots = self._due(), self._draft.has_time, self._slots()
        sent_this_run = 0
        while not self._paid:
            if self._next_at is None:  # 30 days overdue: say so once, then wait in /due for Paid
                if self._state != "parked":
                    self._state = "parked"
                    await self._store("set_next", self._saved())
                    await self._show_reminder("last")
                await workflow.wait_condition(lambda: self._paid)
                break
            if sent_this_run >= CONTINUE_AFTER or workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(ReminderInput(**{**self._inp.__dict__, "carry": self._carry()}))
            if self._snooze_until is not None and self._snooze_until != self._applied:
                self._applied = self._snooze_until
                # A snooze only ever postpones: an earlier time would skip ahead of the schedule.
                if self._applied > self._next_at:
                    self._next_at = self._applied
                    await self._store("set_next", self._saved())
                    await self._show_reminder("snoozed", edit=True)
            wait = (self._next_at - workflow.now()).total_seconds()
            if wait > 0:
                try:
                    await workflow.wait_condition(
                        lambda: self._paid or self._snooze_until != self._applied, timeout=timedelta(seconds=wait)
                    )
                    continue  # Paid or a new snooze: handled at the top of the loop
                except asyncio.TimeoutError:
                    pass
            # Work out the next reminder before sending this one, so the status never shows "sent" with a
            # stale next_at (a Snooze checked against it would be refused). After a long sleep, skip the
            # reminders we slept through instead of sending them in a burst.
            self._next_at = next_reminder(due, has_time, slots, max(self._next_at, workflow.now()))
            self._sent += 1
            sent_this_run += 1
            await self._show_reminder("reminder")
            await self._store("set_next", self._saved())

        self._state = "paid"
        self._paid_at = workflow.now()
        self._next_at = None
        update = PaidUpdate(self._rid(), self._inp.owner_id, self._inp.added_by, self._draft,
                            self._paid_at.isoformat(), self._message_id)  # fmt: skip
        await self._store("mark_paid", update)
        await self._telegram("show_paid", update)

    # Helpers that turn state into activity calls.

    def _rid(self) -> str:
        return workflow.info().workflow_id

    def _due(self) -> datetime:
        return datetime.fromisoformat(self._draft.due_at)

    def _draft_set_at(self) -> datetime:
        return datetime.fromisoformat(self._draft.read_at) if self._draft.read_at else workflow.now()

    def _slots(self) -> list[datetime]:
        return plan(self._due(), self._draft.has_time, self._set_at, self._saved_at)

    def _upcoming(self) -> tuple[list[str], int | None]:
        """The planned reminders still ahead, for messages that state the whole plan. Before Save: as if saved now."""
        due, has_time, now = self._due(), self._draft.has_time, workflow.now()
        set_at = self._set_at or self._draft_set_at()
        slots = plan(due, has_time, set_at, self._saved_at or now)
        ahead = [s for s in slots if s > now]
        if not ahead and (first_overdue := next_reminder(due, has_time, slots, now)):
            ahead = [first_overdue]
        every = demo_every(due, has_time, set_at)
        return [s.isoformat() for s in ahead], int(every.total_seconds()) if every else None

    def _saved(self) -> SavedReminder:
        inp = self._inp
        return SavedReminder(self._rid(), inp.owner_id, inp.added_by, self._draft,
                             self._next_at.isoformat() if self._next_at else None)  # fmt: skip

    def _carry(self) -> Carry:
        return Carry(
            draft=self._draft,
            set_at=self._set_at.isoformat(),
            saved_at=self._saved_at.isoformat(),
            next_at=self._next_at.isoformat() if self._next_at else None,
            sent=self._sent,
            message_id=self._message_id,
            snooze_until=self._snooze_until.isoformat() if self._snooze_until else None,
            snooze_applied=self._applied.isoformat() if self._applied else None,
        )

    async def _store(self, name: str, payload) -> bool:
        try:
            await workflow.execute_activity(name, payload, **STORE)
            return True
        except ActivityError:
            # Each failed attempt is already in Sentry and the logs; the reminder itself carries on.
            # (A failed discard_input leaves a file in .data/tmp, which the start-up sweep deletes.)
            workflow.logger.error("local activity %s failed after %d attempts", name, STORE["retry_policy"].maximum_attempts)
            return False

    async def _telegram(self, name: str, payload, result_type=None):
        try:
            return await workflow.execute_activity(name, payload, result_type=result_type, **TELEGRAM)
        except ActivityError:
            workflow.logger.warning("Telegram activity %s was refused", name)
            return None

    async def _show_confirm(self, state: str) -> None:
        inp = self._inp
        plan_, every = ([], None)
        if self._draft.ready and state != "expired":
            plan_, every = self._upcoming()
        view = ConfirmView(self._rid(), inp.origin_chat_id, inp.status_message_id, self._draft, inp.owner_id,
                           inp.added_by, state, plan_, every)  # fmt: skip
        await self._telegram("show_confirm", view)

    async def _show_reminder(self, reason: str, edit: bool = False) -> None:
        inp = self._inp
        plan_, every = self._upcoming() if reason == "added" else ([], None)
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
            plan=plan_,
            demo_every_s=every,
        )
        name = "edit_reminder" if edit and self._message_id else "send_reminder"
        message_id = await self._telegram(name, view, result_type=int)
        if message_id is None and reason == "added":
            # Telegram won't let a bot message someone who never opened it. Keep the reminder alive,
            # tell whoever added it, and try again at each reminder.
            await self._notice(
                "I couldn't message them on Telegram. Ask them to open the bot and send /start; "
                "I'll keep trying at each reminder."
            )
        self._message_id = message_id or self._message_id

    async def _notice(self, text: str, edit: int | None = None, reply: int | None = None) -> None:
        notice = Notice(chat_id=self._inp.origin_chat_id, text=text, reply_to=reply, edit_message_id=edit)
        await self._telegram("notify", notice)


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
