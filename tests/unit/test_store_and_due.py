from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from paidyet import bot
from paidyet.config import load_settings
from paidyet.store import Store

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=IST)  # Saturday
SETTINGS = load_settings({"ADMIN_USER_ID": "111", "ALLOWED_USERS": "Prateek:111,Arjun:222"})
ADMIN, ARJUN = 111, 222


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "paidyet.db")
    s.save("r-000000000001", ARJUN, ARJUN, "iou", "Dinner split", "Rahul", 860.0, "2026-10-09T00:00:00+05:30", False, "2026-10-11T10:00:00+05:30")
    s.save("r-000000000002", ARJUN, ARJUN, "bill", "Broadband", "SkyNet Fibre", 1179.0, "2026-10-12T00:00:00+05:30", False, "2026-10-11T19:00:00+05:30")
    s.save("r-000000000003", ARJUN, ADMIN, "iou", "Goa trip", "Priya", 3450.0, "2026-10-17T00:00:00+05:30", False, "2026-10-16T19:00:00+05:30")
    s.save("r-000000000004", ADMIN, ADMIN, "bill", "Electricity", "Sahyadri", 1240.0, "2026-10-20T00:00:00+05:30", False, "2026-10-19T19:00:00+05:30")
    yield s
    s.close()


def test_store_keeps_only_fields_and_ids(store):
    columns = {r[1] for r in store._db.execute("PRAGMA table_info(reminders)")}
    assert columns == {"id", "owner_id", "added_by", "kind", "title", "payee", "amount_inr", "due_at", "has_time",
                       "next_at", "created_at", "paid_at"}  # fmt: skip


def test_open_lists_and_paid(store):
    assert [r.id for r in store.open_for(ARJUN)] == ["r-000000000001", "r-000000000002", "r-000000000003"]
    assert [r.id for r in store.open_added_by(ADMIN)] == ["r-000000000003"]
    store.mark_paid("r-000000000001", "2026-10-10T12:00:00+05:30")
    assert [r.id for r in store.open_for(ARJUN)] == ["r-000000000002", "r-000000000003"]
    assert store.get("r-000000000001").next_at is None
    store.set_next("r-000000000002", "2026-10-12T10:00:00+05:30")
    assert store.get("r-000000000002").next_at == "2026-10-12T10:00:00+05:30"


def test_save_is_idempotent_for_activity_retries(store):
    store.save("r-000000000002", ARJUN, ARJUN, "bill", "Broadband", "SkyNet Fibre", 1179.0, "2026-10-12T00:00:00+05:30", False, "2026-10-12T10:00:00+05:30")
    assert len(store.open_for(ARJUN)) == 3


def test_due_for_the_friend(store):
    text, markup = bot.due_message(ARJUN, SETTINGS, store, NOW)
    assert text.splitlines() == [
        "🔴 Overdue",
        "• Dinner split · ₹860 to Rahul · overdue by 1 day (was due Fri 9 Oct) · added by you",
        "",
        "🗓 Upcoming",
        "• Broadband · ₹1,179 to SkyNet Fibre · due Mon 12 Oct, in 2 days · added by you",
        "• Goa trip · ₹3,450 to Priya · due Sat 17 Oct, in 7 days · added by Prateek",
    ]
    assert [row[0].callback_data for row in markup.inline_keyboard] == [
        "dpaid:r-000000000001", "dpaid:r-000000000002", "dpaid:r-000000000003"]  # fmt: skip


def test_due_for_the_admin_shows_what_they_added_for_others(store):
    text, markup = bot.due_message(ADMIN, SETTINGS, store, NOW)
    assert "• Electricity · ₹1,240 to Sahyadri · due Tue 20 Oct, in 10 days · added by you" in text
    assert "👀 You added for others\n• Arjun: Goa trip · ₹3,450 · due Sat 17 Oct, in 7 days" in text
    assert [row[0].callback_data for row in markup.inline_keyboard] == ["dpaid:r-000000000004"]  # only their own


def test_due_skips_the_one_just_paid_and_handles_empty(store):
    text, _ = bot.due_message(ARJUN, SETTINGS, store, NOW, skip="r-000000000001")
    assert "Dinner split" not in text
    for rid in ("r-000000000001", "r-000000000002", "r-000000000003"):
        store.mark_paid(rid, "2026-10-10T12:00:00+05:30")
    assert bot.due_message(ARJUN, SETTINGS, store, NOW) == ("Nothing due. 🎉", None)


@pytest.mark.parametrize(
    "caption, name, rest",
    [("for arjun", "arjun", ""), ("For Arjun: trip share", "Arjun", "trip share"), ("for arjun - due Friday", "arjun", "due Friday")],
)
def test_for_name_captions(caption, name, rest):
    m = bot.FOR_NAME.match(caption)
    assert (m["name"], m["rest"]) == (name, rest)


def test_ordinary_captions_are_not_for_someone():
    assert bot.FOR_NAME.match("electricity bill, pay by friday") is None
