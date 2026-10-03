"""ReminderWorkflow in Temporal's time-skipping test server, with fake activities."""

import asyncio
import contextlib
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from paidyet import workflows
from paidyet.extract import Draft
from paidyet.workflows import (
    ConfirmView,
    FixRequest,
    Notice,
    PaidUpdate,
    ReadRequest,
    ReminderInput,
    ReminderView,
    ReminderWorkflow,
    SavedReminder,
    Status,
    next_reminder,
    plan,
)

IST = ZoneInfo("Asia/Kolkata")
ARJUN, ADMIN = 222, 111


class Fakes:
    """Activities with the real names; records every call. `reads` is what read_input does on each attempt."""

    def __init__(self, reads: list, fixes: list | None = None, telegram_refuses: bool = False, store_fails: bool = False):
        self.reads = list(reads)
        self.fixes = list(fixes or [])
        self.telegram_refuses = telegram_refuses
        self.store_fails = store_fails
        self.calls: list[tuple[str, object]] = []
        self.message_ids = iter(range(100, 1000))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def payloads(self, name: str) -> list:
        return [p for n, p in self.calls if n == name]

    def activities(self) -> list:
        @activity.defn(name="read_input")
        async def read_input(req: ReadRequest) -> Draft:
            self.calls.append(("read_input", req))
            outcome = self.reads.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        @activity.defn(name="discard_input")
        async def discard_input(req: ReadRequest) -> None:
            self.calls.append(("discard_input", req))

        @activity.defn(name="fix_draft")
        async def fix_draft(req: FixRequest) -> Draft:
            self.calls.append(("fix_draft", req))
            return self.fixes.pop(0)

        @activity.defn(name="show_confirm")
        async def show_confirm(view: ConfirmView) -> None:
            self.calls.append(("show_confirm", view))

        @activity.defn(name="send_reminder")
        async def send_reminder(view: ReminderView) -> int:
            self.calls.append(("send_reminder", view))
            if self.telegram_refuses:
                raise ApplicationError("the user blocked the bot", type="Forbidden", non_retryable=True)
            return next(self.message_ids)

        @activity.defn(name="edit_reminder")
        async def edit_reminder(view: ReminderView) -> int:
            self.calls.append(("edit_reminder", view))
            return view.previous_message_id

        @activity.defn(name="save_reminder")
        async def save_reminder(saved: SavedReminder) -> None:
            self.calls.append(("save_reminder", saved))
            if self.store_fails:
                raise RuntimeError("database is locked")

        @activity.defn(name="set_next")
        async def set_next(saved: SavedReminder) -> None:
            self.calls.append(("set_next", saved))

        @activity.defn(name="mark_paid")
        async def mark_paid(update: PaidUpdate) -> None:
            self.calls.append(("mark_paid", update))

        @activity.defn(name="show_paid")
        async def show_paid(update: PaidUpdate) -> None:
            self.calls.append(("show_paid", update))

        @activity.defn(name="notify")
        async def notify(notice: Notice) -> None:
            self.calls.append(("notify", notice))

        return [read_input, discard_input, fix_draft, show_confirm, send_reminder, edit_reminder,
                save_reminder, set_next, mark_paid, show_paid, notify]  # fmt: skip


def draft(due: datetime, has_time: bool = False, **kw) -> Draft:
    if not has_time:
        due = due.replace(hour=0, minute=0, second=0, microsecond=0)
    base = Draft(kind="iou", title="Dinner split", payee="Rahul", amount_inr=860.0, due_at=due.isoformat(),
                 has_time=has_time, confidence=0.9)  # fmt: skip
    return replace(base, **kw)


async def now_ist(env: WorkflowEnvironment) -> datetime:
    return (await env.get_current_time()).astimezone(IST)


async def start(env, fakes: Fakes, *, added_by: int = ARJUN, sent_at: datetime | None = None, **worker_kw):
    queue = f"test-{uuid.uuid4()}"
    worker = Worker(env.client, task_queue=queue, workflows=[ReminderWorkflow], activities=fakes.activities(), **worker_kw)
    inp = ReminderInput(
        owner_id=ARJUN, added_by=added_by, origin_chat_id=added_by, origin_message_id=1, status_message_id=2,
        sent_at=(sent_at or await now_ist(env)).isoformat(), text_key="k" * 32,
    )  # fmt: skip
    handle = await env.client.start_workflow(ReminderWorkflow.run, inp, id=f"r-{uuid.uuid4().hex[:12]}", task_queue=queue)
    return worker, handle


@contextlib.asynccontextmanager
async def running(env, fakes: Fakes, **kw):
    """Start a workflow with its own worker; terminate it at the end so no stray timer outlives the test."""
    worker, handle = await start(env, fakes, **kw)
    async with worker:
        try:
            yield handle
        finally:
            await settle(fakes)
            with contextlib.suppress(Exception):  # by ID, so a continued-as-new run is stopped too
                await env.client.get_workflow_handle(handle.id).terminate("test finished")


async def settle(fakes: Fakes, quiet: float = 0.2, limit: float = 3.0) -> None:
    """Wait until no activity has started for `quiet` seconds. Terminating a workflow mid-activity
    leaves a time-skipping lock held in the test server, and the next test's env.sleep() then hangs."""
    seen, waited = len(fakes.calls), 0.0
    while waited < limit:
        await asyncio.sleep(quiet)
        waited += quiet
        if len(fakes.calls) == seen:
            return
        seen = len(fakes.calls)


async def until(handle: WorkflowHandle, check, tries: int = 100) -> Status:
    """Poll the status query (real time, a few ms) until `check(status)` holds."""
    for _ in range(tries):
        status = await handle.query(ReminderWorkflow.status)
        if check(status):
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"workflow never reached the expected state; last: {status}")


async def eventually(check, tries: int = 100) -> None:
    for _ in range(tries):
        if check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition never held")


async def test_save_remind_snooze_remind_paid(env):
    now = await now_ist(env)
    due = now + timedelta(days=5)
    fakes = Fakes(reads=[draft(due)])
    async with running(env, fakes) as handle:
        await until(handle, lambda s: s.state == "confirming")
        assert fakes.names()[:3] == ["read_input", "discard_input", "show_confirm"]
        assert fakes.payloads("show_confirm")[0].state == "draft"

        await handle.signal(ReminderWorkflow.save)
        status = await until(handle, lambda s: s.state == "scheduled" and s.next_at)
        first = datetime.fromisoformat(status.next_at)
        assert (first.date(), first.hour) == ((now + timedelta(days=2)).date(), 10)  # 10 AM, 3 days before
        await eventually(lambda: fakes.payloads("show_confirm")[-1].state == "saved")
        assert fakes.payloads("save_reminder")[0].next_at == status.next_at

        await env.sleep(first - await env.get_current_time() + timedelta(seconds=1))
        await until(handle, lambda s: s.reminders_sent == 1)
        assert fakes.payloads("send_reminder")[0].reason == "reminder"

        # Snooze past the evening-before and due-day morning reminders: they're skipped.
        snooze_to = datetime.combine(due.date(), datetime.min.time(), tzinfo=IST).replace(hour=12)
        await handle.signal(ReminderWorkflow.snooze, snooze_to.isoformat())
        await until(handle, lambda s: s.next_at == snooze_to.isoformat())
        await eventually(lambda: fakes.payloads("edit_reminder"))
        assert fakes.payloads("edit_reminder")[0].reason == "snoozed"

        await env.sleep(snooze_to - await env.get_current_time() + timedelta(seconds=1))
        status = await until(handle, lambda s: s.reminders_sent == 2)
        assert datetime.fromisoformat(status.next_at).hour == 19  # then the due day's last call

        await handle.signal(ReminderWorkflow.paid)
        assert await handle.result() == "paid"
    assert fakes.payloads("mark_paid")[0].reminder_id == handle.id
    assert fakes.payloads("show_paid")[0].message_id == 101  # the latest reminder message gets "✅ Paid on …"
    assert fakes.names().count("discard_input") == 1


async def test_flaky_reads_are_retried(env):
    now = await now_ist(env)
    fakes = Fakes(reads=[RuntimeError("Ollama timed out"), RuntimeError("bad JSON"), draft(now + timedelta(days=3))])
    async with running(env, fakes) as handle:
        await eventually(lambda: fakes.names().count("read_input") == 1)
        await env.sleep(timedelta(seconds=10))  # skip the retry backoff (2 s, then 4 s)
        await until(handle, lambda s: s.state == "confirming")
    assert fakes.names().count("read_input") == 3
    assert fakes.names().count("discard_input") == 1  # deleted once, after the read finally succeeded


async def test_final_read_failure_still_deletes_the_input(env):
    fakes = Fakes(reads=[RuntimeError("down")] * 3)
    async with running(env, fakes) as handle:
        assert await handle.result() == "failed"
    assert fakes.names() == ["read_input"] * 3 + ["discard_input", "notify"]
    assert "couldn't read" in fakes.payloads("notify")[0].text


async def test_input_gone_after_a_restart_is_not_retried(env):
    gone = ApplicationError("the photo is gone", type="InputGone", non_retryable=True)
    fakes = Fakes(reads=[gone])
    async with running(env, fakes) as handle:
        assert await handle.result() == "failed"
    assert fakes.names().count("read_input") == 1
    assert "discard_input" in fakes.names()


async def test_fix_then_save(env):
    now = await now_ist(env)
    broken = draft(now + timedelta(days=3), amount_inr=None, problems=["I couldn't find the amount."])
    fixed = draft(now + timedelta(days=3), amount_inr=1240.0)
    fakes = Fakes(reads=[broken], fixes=[fixed])
    async with running(env, fakes) as handle:
        await until(handle, lambda s: s.state == "confirming")
        await handle.signal(ReminderWorkflow.save)  # not ready yet: ignored
        await handle.signal(ReminderWorkflow.fix, "c" * 32)
        status = await until(handle, lambda s: s.state == "confirming" and s.draft.ready)
        assert status.draft.amount_inr == 1240.0
        assert fakes.payloads("fix_draft")[0].correction_key == "c" * 32
        assert fakes.payloads("fix_draft")[0].previous.amount_inr is None
        await handle.signal(ReminderWorkflow.save)
        await until(handle, lambda s: s.state == "scheduled")
        await handle.signal(ReminderWorkflow.paid)
        assert await handle.result() == "paid"
    assert [v.draft.ready for v in fakes.payloads("show_confirm")] == [False, True, True]


async def test_overdue_nudges_daily_until_paid(env):
    now = await now_ist(env)
    due = now + timedelta(days=1)
    fakes = Fakes(reads=[draft(due)])
    async with running(env, fakes) as handle:
        await until(handle, lambda s: s.state == "confirming")
        await handle.signal(ReminderWorkflow.save)
        await until(handle, lambda s: s.state == "scheduled")
        await env.sleep(timedelta(days=4))
        status = await until(handle, lambda s: s.reminders_sent >= 3)
        await handle.signal(ReminderWorkflow.paid)
        assert await handle.result() == "paid"
    sent = [datetime.fromisoformat(v.now).astimezone(IST) for v in fakes.payloads("send_reminder")]
    due_day = datetime.fromisoformat(draft(due).due_at)
    slots, expected, after = plan(due_day, False, now, now), [], now
    while len(expected) < len(sent):
        after = next_reminder(due_day, False, slots, after)
        expected.append(after)
    assert [t.replace(second=0, microsecond=0) for t in sent] == expected
    assert sent[-1].hour == 10 and sent[-1].date() > due.date()  # overdue: every morning
    assert status.reminders_sent >= 3


async def test_demo_due_repeats_five_times_then_goes_daily(env):
    now = await now_ist(env)
    fakes = Fakes(reads=[draft(now + timedelta(minutes=2), has_time=True, read_at=now.isoformat())])
    async with running(env, fakes, sent_at=now) as handle:
        await until(handle, lambda s: s.state == "confirming")
        await handle.signal(ReminderWorkflow.save)
        await until(handle, lambda s: s.state == "scheduled")
        await env.sleep(timedelta(minutes=11))
        status = await until(handle, lambda s: s.reminders_sent == 5)
        assert datetime.fromisoformat(status.next_at).astimezone(IST).hour == 10  # then every morning
        await handle.signal(ReminderWorkflow.paid)
        assert await handle.result() == "paid"
    times = [datetime.fromisoformat(v.now) for v in fakes.payloads("send_reminder")]
    assert len(times) == 5
    assert {round((b - a).total_seconds() / 60) for a, b in zip(times, times[1:])} == {2}
    confirm = fakes.payloads("show_confirm")[0]
    assert confirm.demo_every_s == 120 and len(confirm.plan) == 5


async def test_admin_added_reminder_tells_the_friend_and_reports_paid(env):
    now = await now_ist(env)
    fakes = Fakes(reads=[draft(now + timedelta(days=5), title="Goa trip", payee="Priya", amount_inr=3450.0)])
    async with running(env, fakes, added_by=ADMIN) as handle:
        await until(handle, lambda s: s.state == "confirming")
        assert fakes.payloads("show_confirm")[0].chat_id == ADMIN  # the admin confirms what they added
        await handle.signal(ReminderWorkflow.save)
        await until(handle, lambda s: s.state == "scheduled" and s.message_id)
        added = fakes.payloads("send_reminder")[0]
        assert (added.reason, added.chat_id, added.added_by) == ("added", ARJUN, ADMIN)
        assert added.plan  # the notice states the whole plan
        await handle.signal(ReminderWorkflow.paid)
        assert await handle.result() == "paid"
    paid = fakes.payloads("show_paid")[0]
    assert (paid.owner_id, paid.added_by) == (ARJUN, ADMIN)


async def test_unconfirmed_draft_expires_after_a_week(env):
    now = await now_ist(env)
    fakes = Fakes(reads=[draft(now + timedelta(days=30))])
    async with running(env, fakes) as handle:
        assert await handle.result() == "expired"
    assert fakes.payloads("show_confirm")[-1].state == "expired"
    assert "save_reminder" not in fakes.names()


async def test_friend_who_never_opened_the_bot(env):
    now = await now_ist(env)
    fakes = Fakes(reads=[draft(now + timedelta(days=5))], telegram_refuses=True)
    async with running(env, fakes, added_by=ADMIN) as handle:
        await until(handle, lambda s: s.state == "confirming")
        await handle.signal(ReminderWorkflow.save)
        await eventually(lambda: fakes.payloads("notify"))
        notice = fakes.payloads("notify")[0]
        assert notice.chat_id == ADMIN and "/start" in notice.text
        status = await until(handle, lambda s: s.state == "scheduled")
        assert status.next_at  # still alive: the next reminder will try again


async def test_one_sentry_trace_from_the_telegram_update_to_every_activity(env, sentry):
    import sentry_sdk
    from temporalio.client import Client

    from main import SentryClientInterceptor, SentryWorkerInterceptor

    client = Client(**{**env.client.config(), "interceptors": [SentryClientInterceptor()]})
    now = await now_ist(env)
    fakes = Fakes(reads=[draft(now + timedelta(days=3))])
    queue = f"test-{uuid.uuid4()}"
    worker = Worker(client, task_queue=queue, workflows=[ReminderWorkflow], activities=fakes.activities(),
                    interceptors=[SentryWorkerInterceptor()])  # fmt: skip
    inp = ReminderInput(owner_id=ARJUN, added_by=ARJUN, origin_chat_id=ARJUN, origin_message_id=1,
                        status_message_id=2, sent_at=now.isoformat(), text_key="k" * 32)  # fmt: skip
    async with worker:
        with sentry_sdk.start_transaction(op="telegram.update", name="telegram capture") as parent:
            handle = await client.start_workflow(ReminderWorkflow.run, inp, id=f"r-{uuid.uuid4().hex[:12]}", task_queue=queue)
        try:
            await until(handle, lambda s: s.state == "confirming")
            await eventually(lambda: "show_confirm" in fakes.names())
        finally:
            await handle.terminate("test finished")
    traces = {t["transaction"]: t["contexts"]["trace"]["trace_id"] for t in sentry.transactions()}
    assert {"activity read_input", "activity discard_input", "activity show_confirm"} <= traces.keys()
    assert set(traces.values()) == {parent.trace_id}


async def test_a_snooze_never_moves_the_next_reminder_earlier(env):
    now = await now_ist(env)
    fakes = Fakes(reads=[draft(now + timedelta(days=10))])
    async with running(env, fakes, added_by=ADMIN) as handle:
        await until(handle, lambda s: s.state == "confirming")
        await handle.signal(ReminderWorkflow.save)
        status = await until(handle, lambda s: s.state == "scheduled" and s.message_id)
        planned = status.next_at  # 10 AM, 3 days before
        earlier = (now + timedelta(days=1)).isoformat()
        await handle.signal(ReminderWorkflow.snooze, earlier)
        await asyncio.sleep(0.3)
        assert (await handle.query(ReminderWorkflow.status)).next_at == planned
        assert "edit_reminder" not in fakes.names()
        later = (datetime.fromisoformat(planned) + timedelta(days=1)).isoformat()
        await handle.signal(ReminderWorkflow.snooze, later)
        await until(handle, lambda s: s.next_at == later)


async def test_sqlite_failures_are_bounded_and_surfaced(env):
    now = await now_ist(env)
    fakes = Fakes(reads=[draft(now + timedelta(days=5))], store_fails=True)
    async with running(env, fakes) as handle:
        await until(handle, lambda s: s.state == "confirming")
        await handle.signal(ReminderWorkflow.save)
        await eventually(lambda: fakes.names().count("save_reminder") == 1)
        await env.sleep(timedelta(minutes=1))  # skip the retry backoff
        await eventually(lambda: fakes.payloads("notify"))
        assert "couldn't add it to /due" in fakes.payloads("notify")[0].text
        assert fakes.names().count("save_reminder") == 5
        await until(handle, lambda s: s.state == "scheduled")  # the reminder itself carries on


async def test_after_30_days_overdue_it_says_so_once_and_waits_in_due(env):
    now = await now_ist(env)
    fakes = Fakes(reads=[draft(now + timedelta(days=1))])
    async with running(env, fakes) as handle:
        await until(handle, lambda s: s.state == "confirming")
        await handle.signal(ReminderWorkflow.save)
        await until(handle, lambda s: s.state == "scheduled")
        await env.sleep(timedelta(days=33))
        status = await until(handle, lambda s: s.state == "parked")
        assert status.next_at is None
        await eventually(lambda: fakes.payloads("send_reminder")[-1].reason == "last")
        assert fakes.payloads("set_next")[-1].next_at is None
        sent = len(fakes.payloads("send_reminder"))
        await env.sleep(timedelta(days=10))
        assert len(fakes.payloads("send_reminder")) == sent  # quiet from here on
        await env.client.get_workflow_handle(handle.id).signal(ReminderWorkflow.paid)
        assert await handle.result() == "paid"


async def test_continue_as_new_carries_the_reminder_over(env, monkeypatch):
    # Unsandboxed so the lowered threshold reaches the workflow code.
    monkeypatch.setattr(workflows, "CONTINUE_AFTER", 2)
    now = await now_ist(env)
    fakes = Fakes(reads=[draft(now + timedelta(minutes=2), has_time=True, read_at=now.isoformat())])
    async with running(env, fakes, sent_at=now, workflow_runner=UnsandboxedWorkflowRunner()) as handle:
        latest = env.client.get_workflow_handle(handle.id)
        await until(latest, lambda s: s.state == "confirming")
        await latest.signal(ReminderWorkflow.save)
        await until(latest, lambda s: s.state == "scheduled")
        await env.sleep(timedelta(minutes=11))
        status = await until(latest, lambda s: s.reminders_sent == 5)
        assert (await latest.describe()).run_id != handle.run_id  # it's a later run now
        assert status.draft.amount_inr == 860.0 and status.message_id == 104
        await latest.signal(ReminderWorkflow.paid)
        assert await handle.result() == "paid"
    assert fakes.names().count("read_input") == 1 and fakes.names().count("save_reminder") == 1
    assert fakes.payloads("show_paid")[0].message_id == 104
