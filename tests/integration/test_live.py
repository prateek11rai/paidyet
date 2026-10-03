"""The real stack: Gemma on the samples, and full reminder cycles driven by signals as if buttons were tapped."""

import asyncio
import json
import uuid
from datetime import datetime, timedelta

import sentry_sdk

from paidyet import extract
from paidyet.config import ROOT
from paidyet.workflows import ReminderInput, ReminderWorkflow, Status

from .conftest import PREFIX

SAMPLES = json.loads((ROOT / "samples" / "expected.json").read_text())


async def test_gemma_reads_every_sample_correctly_or_refuses_to_save(settings, http):
    """The bar: every field right, or the draft blocked from Save. Never a silently wrong due date."""
    now = datetime.fromisoformat(SAMPLES["now"])
    for case in SAMPLES["cases"]:
        image = (ROOT / "samples" / case["file"]).read_bytes() if "file" in case else None
        draft, usages = await extract.read(http, settings.ollama_model, now, text=case.get("text"), image=image)
        label = case.get("file") or case["text"]
        assert draft.kind == case["kind"], label
        assert draft.amount_inr == case["amount_inr"], label
        assert draft.payee is None or case["payee"] in draft.payee.lower(), label
        if draft.ready:
            due = datetime.fromisoformat(draft.due_at)
            if case["due"] == "+2m":
                assert draft.has_time and abs(due - (now + timedelta(minutes=2))) < timedelta(seconds=1), label
            else:
                assert due.date().isoformat() == case["due"], label
        else:
            assert draft.due_at is None, f"{label}: blocked drafts must not carry a date"
        assert 1 <= len(usages) <= 2


async def _status_message(telegram, settings) -> int:
    return (await telegram.send_message(settings.admin_user_id, PREFIX + "👀 Reading it…")).message_id


async def _start(temporal, worker, settings, telegram, *, text=None, photo_name=None):
    inp = ReminderInput(
        owner_id=settings.admin_user_id,
        added_by=settings.admin_user_id,
        origin_chat_id=settings.admin_user_id,
        origin_message_id=0,
        status_message_id=await _status_message(telegram, settings),
        sent_at=datetime.now(settings.tz).isoformat(),
        photo_name=photo_name,
        text_key=extract.remember_text(text) if text else None,
    )
    rid = f"r-{uuid.uuid4().hex[:12]}"
    with sentry_sdk.start_transaction(op="test", name="integration capture"):
        handle = await temporal.start_workflow(ReminderWorkflow.run, inp, id=rid, task_queue=worker.task_queue)
    return handle, inp


async def until(handle, check, seconds: float = 90) -> Status:
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        status = await handle.query(ReminderWorkflow.status)
        if check(status):
            return status
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out; last status: {status}")
        await asyncio.sleep(0.5)


async def test_text_save_remind_snooze_remind_paid(settings, temporal, worker, telegram, store):
    handle, inp = await _start(temporal, worker, settings, telegram, text="Rahul ko 500 dene hai in 5 seconds")
    try:
        status = await until(handle, lambda s: s.state == "confirming")
        d = status.draft
        assert (d.payee, d.amount_inr, d.has_time, d.ready) == ("Rahul", 500.0, True, True)
        assert extract.recall_text(inp.text_key) is None  # the message text is gone once read

        await handle.signal(ReminderWorkflow.save)
        await until(handle, lambda s: s.state == "scheduled")
        row = store.get(handle.id)
        assert (row.title, row.amount_inr, row.paid_at) == (d.title, 500.0, None)

        status = await until(handle, lambda s: s.reminders_sent == 1, seconds=30)
        assert datetime.fromisoformat(status.next_at) > datetime.fromisoformat(d.due_at)  # already moved on
        # A snooze only postpones, so push past the next demo reminder (a minute after the first).
        snooze_to = datetime.fromisoformat(status.next_at) + timedelta(seconds=5)
        await handle.signal(ReminderWorkflow.snooze, snooze_to.isoformat())
        await until(handle, lambda s: s.next_at == snooze_to.isoformat())
        await until(handle, lambda s: s.reminders_sent == 2, seconds=90)

        await handle.signal(ReminderWorkflow.paid)
        assert await handle.result() == "paid"
        assert store.get(handle.id).paid_at is not None
    finally:
        await _terminate(handle)


async def test_photo_is_read_then_deleted(settings, temporal, worker, telegram):
    name = extract.save_photo(settings.tmp_dir, (ROOT / "samples" / "electricity.png").read_bytes(), ".png")
    handle, _ = await _start(temporal, worker, settings, telegram, photo_name=name)
    try:
        status = await until(handle, lambda s: s.state == "confirming")
        assert status.draft.amount_inr == 1240.0
        assert not extract.photo_path(settings.tmp_dir, name).exists()
    finally:
        await _terminate(handle)


async def test_fix_applies_a_typed_correction(settings, temporal, worker, telegram):
    handle, _ = await _start(temporal, worker, settings, telegram, text="dinner split 860 to Rahul by tomorrow")
    try:
        await until(handle, lambda s: s.state == "confirming")
        key = extract.remember_text("amount is 940")
        await handle.signal(ReminderWorkflow.fix, key)
        status = await until(handle, lambda s: s.state == "confirming" and s.draft.amount_inr == 940.0)
        assert status.draft.payee == "Rahul"
        assert extract.recall_text(key) is None
    finally:
        await _terminate(handle)


def test_no_photos_left_behind(settings):
    assert [p.name for p in settings.tmp_dir.iterdir()] == []


async def _terminate(handle) -> None:
    try:
        await handle.terminate("integration test finished")
    except Exception:
        pass  # already completed
