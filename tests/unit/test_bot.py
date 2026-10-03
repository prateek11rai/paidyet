from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from paidyet import bot
from paidyet.config import load_settings
from paidyet.extract import Draft
from paidyet.workflows import ConfirmView, ReminderView, first_reminder, next_nudge, nudge_interval

IST = ZoneInfo("Asia/Kolkata")
SUNDAY = datetime(2026, 10, 4, 10, 0, tzinfo=IST)
SETTINGS = load_settings({"ADMIN_USER_ID": "111", "ALLOWED_USERS": "Prateek:111,Arjun:222"})
RID = "r-0123456789ab"


def draft(**kw) -> Draft:
    base = dict(kind="bill", title="Electricity", payee="Sahyadri Power", amount_inr=1240.0,
                due_at="2026-10-09T00:00:00+05:30", has_time=False, confidence=0.9)  # fmt: skip
    return Draft(**base | kw)


@pytest.mark.parametrize(
    "amount, text",
    [(1240, "₹1,240"), (500, "₹500"), (15500, "₹15,500"), (100000, "₹1,00,000"),
     (12345678, "₹1,23,45,678"), (99.5, "₹99.50"), (None, "₹?")],
)  # fmt: skip
def test_inr_uses_indian_grouping(amount, text):
    assert bot.inr(amount) == text


def test_confirm_message_matches_the_spec():
    view = ConfirmView(RID, 222, 5, draft(), owner_id=222, added_by=222, remind_at="2026-10-08T19:00:00+05:30", state="draft")
    text = bot.confirm_text(view, SETTINGS, SUNDAY)
    assert text.splitlines()[0] == "Electricity · ₹1,240 to Sahyadri Power · due Fri 9 Oct"
    assert "added by you" in text and "I'll remind you Thu 8 Oct, 7 PM." in text


def test_confirm_for_a_friend_says_who_added_it():
    view = ConfirmView(RID, 111, 5, draft(), owner_id=222, added_by=111, remind_at=None, state="saved")
    text = bot.confirm_text(view, SETTINGS, SUNDAY)
    assert text.startswith("✅ Saved: ") and "For Arjun · added by you" in text


def test_problems_hide_save():
    d = draft(amount_inr=None, problems=["I couldn't find the amount."])
    view = ConfirmView(RID, 222, 5, d, 222, 222, None, "draft")
    assert "⚠️ I couldn't find the amount." in bot.confirm_text(view, SETTINGS, SUNDAY)
    buttons = [b.text for row in bot.confirm_keyboard(RID, d).inline_keyboard for b in row]
    assert buttons == ["✏️ Fix"]


def test_reminders_say_who_added_them_and_how_late():
    view = ReminderView(RID, 222, draft(), owner_id=222, added_by=111, reason="reminder", now="2026-10-11T10:00:00+05:30")
    text = bot.reminder_text(view, SETTINGS)
    assert "Overdue by 2 days (was due Fri 9 Oct)" in text and "added by Prateek" in text
    view = ReminderView(RID, 222, draft(), 222, 222, "reminder", now="2026-10-08T19:00:00+05:30")
    assert "Due tomorrow, Fri 9 Oct · added by you" in bot.reminder_text(view, SETTINGS)


def test_admin_added_notice():
    view = ReminderView(RID, 222, draft(title="Goa trip", payee="Priya", amount_inr=3450.0), 222, 111, "added",
                        now="2026-10-04T10:00:00+05:30", next_at="2026-10-08T19:00:00+05:30")  # fmt: skip
    assert bot.reminder_text(view, SETTINGS).startswith("Prateek added a reminder for you:\nGoa trip · ₹3,450 to Priya")


def test_callback_data_fits_telegram_limit():
    markups = [bot.confirm_keyboard(RID, draft()), bot.reminder_keyboard(RID), bot.snooze_keyboard(RID),
               bot.calendar_keyboard(RID, 2026, 12, date(2026, 10, 4))]  # fmt: skip
    for markup in markups:
        for row in markup.inline_keyboard:
            for b in row:
                assert len(b.callback_data.encode()) <= 64


def test_calendar_only_offers_future_days():
    kb = bot.calendar_keyboard(RID, 2026, 10, date(2026, 10, 4))
    days = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data.startswith("day:")]
    assert days[0] == f"day:{RID}:20261005" and days[-1] == f"day:{RID}:20261031"


# --- schedule rules (pure functions in workflows.py) ---


def test_first_reminder_is_7pm_the_day_before():
    due = datetime(2026, 10, 9, tzinfo=IST)
    assert first_reminder(due, False, SUNDAY) == datetime(2026, 10, 8, 19, 0, tzinfo=IST)


def test_first_reminder_when_it_is_already_late():
    due = datetime(2026, 10, 5, tzinfo=IST)
    assert first_reminder(due, False, datetime(2026, 10, 4, 21, 0, tzinfo=IST)) == datetime(2026, 10, 5, 10, 0, tzinfo=IST)
    late = datetime(2026, 10, 5, 20, 0, tzinfo=IST)
    assert first_reminder(due, False, late) == late + timedelta(minutes=1)


def test_nudges_daily_at_10_after_the_first_reminder():
    due = datetime(2026, 10, 9, tzinfo=IST)
    fired = datetime(2026, 10, 8, 19, 0, tzinfo=IST)
    assert next_nudge(due, False, fired, timedelta(days=1)) == datetime(2026, 10, 9, 10, 0, tzinfo=IST)
    assert next_nudge(due, False, datetime(2026, 10, 9, 10, 0, tzinfo=IST), timedelta(days=1)) == datetime(2026, 10, 10, 10, 0, tzinfo=IST)


def test_demo_nudge_interval():
    sent = SUNDAY
    assert nudge_interval(sent + timedelta(minutes=2), True, sent) == timedelta(minutes=2)
    assert nudge_interval(sent + timedelta(seconds=10), True, sent) == timedelta(minutes=1)
    assert nudge_interval(sent + timedelta(days=3), True, sent) == timedelta(days=1)
    assert nudge_interval(sent + timedelta(days=3), False, sent) == timedelta(days=1)
