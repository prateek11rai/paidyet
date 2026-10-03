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
from temporalio.worker import Worker

from paidyet.config import ROOT
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
)

IST = ZoneInfo("Asia/Kolkata")
ARJUN, ADMIN = 222, 111


@pytest.fixture(scope="session")
async def env():
    cache = ROOT / ".pytest_cache" / "temporal"  # the test server binary stays inside the repo
    cache.mkdir(parents=True, exist_ok=True)
    async with await WorkflowEnvironment.start_time_skipping(download_dest_dir=str(cache)) as env:
        yield env


class Fakes:
    """Activities with the real names; records every call. `reads` is what read_input does on each attempt."""

    def __init__(self, reads: list, fixes: list | None = None):
        self.reads = list(reads)
        self.fixes = list(fixes or [])
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
            return next(self.message_ids)

        @activity.defn(name="edit_reminder")
        async def edit_reminder(view: ReminderView) -> int:
            self.calls.append(("edit_reminder", view))
            return view.previous_message_id

        @activity.defn(name="save_reminder")
        async def save_reminder(saved: SavedReminder) -> None:
            self.calls.append(("save_reminder", saved))

        @activity.defn(name="set_next")
        async def set_next(saved: SavedReminder) -> None:
            self.calls.append(("set_next", saved))

        @activity.defn(name="mark_paid")
        async def mark_paid(update: PaidUpdate) -> None:
            self.calls.append(("mark_paid", update))

        @activity.defn(name="notify")
        async def notify(notice: Notice) -> None:
            self.calls.append(("notify", notice))

        return [read_input, discard_input, fix_draft, show_confirm, send_reminder, edit_reminder,
                save_reminder, set_next, mark_paid, notify]  # fmt: skip


def draft(due: datetime, has_time: bool = False, **kw) -> Draft:
    if not has_time:
        due = due.replace(hour=0, minute=0, second=0, microsecond=0)
    base = Draft(kind="iou", title="Dinner split", payee="Rahul", amount_inr=860.0, due_at=due.isoformat(),
                 has_time=has_time, confidence=0.9)  # fmt: skip
    return replace(base, **kw)


async def now_ist(env: WorkflowEnvironment) -> datetime:
    return (await env.get_current_time()).astimezone(IST)


async def start(env, fakes: Fakes, *, added_by: int = ARJUN, sent_at: datetime | None = None):
    queue = f"test-{uuid.uuid4()}"
    worker = Worker(env.client, task_queue=queue, workflows=[ReminderWorkflow], activities=fakes.activities())
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
            with contextlib.suppress(Exception):
                await handle.terminate("test finished")


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
    fakes = Fakes(reads=[draft(now + timedelta(days=3))])
    async with running(env, fakes) as handle:
        await until(handle, lambda s: s.state == "confirming")
        assert fakes.names()[:3] == ["read_input", "discard_input", "show_confirm"]
        assert fakes.payloads("show_confirm")[0].state == "draft"

        await handle.signal(ReminderWorkflow.save)
        status = await until(handle, lambda s: s.state == "scheduled" and s.next_at)
        first = datetime.fromisoformat(status.next_at)
        assert (first.date(), first.hour) == ((now + timedelta(days=2)).date(), 19)  # 7 PM the day before
        await eventually(lambda: fakes.payloads("show_confirm")[-1].state == "saved")
        assert fakes.payloads("save_reminder")[0].next_at == status.next_at

        await env.sleep(first - await env.get_current_time() + timedelta(seconds=1))
        await until(handle, lambda s: s.reminders_sent == 1)
        assert fakes.payloads("send_reminder")[0].reason == "reminder"

        snooze_to = (await now_ist(env)) + timedelta(days=1)
        await handle.signal(ReminderWorkflow.snooze, snooze_to.isoformat())
        await until(handle, lambda s: s.next_at == snooze_to.isoformat())
        await eventually(lambda: fakes.payloads("edit_reminder"))
        assert fakes.payloads("edit_reminder")[0].reason == "snoozed"

        await env.sleep(timedelta(days=1, seconds=1))
        await until(handle, lambda s: s.reminders_sent == 2)

        await handle.signal(ReminderWorkflow.paid)
        assert await handle.result() == "paid"
    paid = fakes.payloads("mark_paid")[0]
    assert paid.message_id == 101  # the latest reminder message gets "✅ Paid on …"
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
    times = [datetime.fromisoformat(v.now).astimezone(IST) for v in fakes.payloads("send_reminder")]
    due_day = due.date()
    expected = [(due_day - timedelta(days=1), 19), (due_day, 10), (due_day + timedelta(days=1), 10)]
    if now.hour >= 19:  # saved after 7 PM the day before: the first reminder is 10 AM on the due day
        expected = expected[1:]
    assert [(t.date(), t.hour) for t in times[: len(expected)]] == expected
    assert status.reminders_sent >= 3


async def test_demo_due_in_two_minutes_repeats_every_two_minutes(env):
    now = await now_ist(env)
    fakes = Fakes(reads=[draft(now + timedelta(minutes=2), has_time=True)])
    async with running(env, fakes, sent_at=now) as handle:
        await until(handle, lambda s: s.state == "confirming")
        await handle.signal(ReminderWorkflow.save)
        await until(handle, lambda s: s.state == "scheduled")
        await env.sleep(timedelta(minutes=6, seconds=30))
        await until(handle, lambda s: s.reminders_sent >= 3)
        await handle.signal(ReminderWorkflow.paid)
        assert await handle.result() == "paid"
    times = [datetime.fromisoformat(v.now) for v in fakes.payloads("send_reminder")]
    gaps = {round((b - a).total_seconds() / 60) for a, b in zip(times, times[1:])}
    assert gaps == {2}


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
        await handle.signal(ReminderWorkflow.paid)
        assert await handle.result() == "paid"
    paid = fakes.payloads("mark_paid")[0]
    assert (paid.owner_id, paid.added_by) == (ARJUN, ADMIN)


async def test_unconfirmed_draft_expires_after_a_week(env):
    now = await now_ist(env)
    fakes = Fakes(reads=[draft(now + timedelta(days=30))])
    async with running(env, fakes) as handle:
        assert await handle.result() == "expired"
    assert fakes.payloads("show_confirm")[-1].state == "expired"
    assert "save_reminder" not in fakes.names()
