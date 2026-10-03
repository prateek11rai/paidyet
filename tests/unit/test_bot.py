from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from paidyet import bot
from paidyet.config import load_settings
from paidyet.extract import Draft
from paidyet.workflows import ConfirmView, ReminderView, demo_every, next_reminder, plan

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


def test_confirm_message_states_the_whole_plan():
    planned = ["2026-10-06T10:00:00+05:30", "2026-10-08T19:00:00+05:30", "2026-10-09T10:00:00+05:30", "2026-10-09T19:00:00+05:30"]
    view = ConfirmView(RID, 222, 5, draft(), owner_id=222, added_by=222, state="draft", plan=planned)
    text = bot.confirm_text(view, SETTINGS, SUNDAY)
    assert text.splitlines()[0] == "Electricity · ₹1,240 to Sahyadri Power · due Fri 9 Oct"
    assert "added by you" in text
    assert "I'll remind you Tue 6 Oct 10 AM, Thu 8 Oct 7 PM, Fri 9 Oct 10 AM and 7 PM, then every morning until it's paid." in text


def test_confirm_for_a_friend_says_who_added_it():
    view = ConfirmView(RID, 111, 5, draft(), owner_id=222, added_by=111, state="saved")
    text = bot.confirm_text(view, SETTINGS, SUNDAY)
    assert text.startswith("✅ Saved: ") and "For Arjun · added by you" in text


def test_problems_hide_save():
    d = draft(amount_inr=None, problems=["I couldn't find the amount."])
    view = ConfirmView(RID, 222, 5, d, 222, 222, "draft")
    assert "⚠️ I couldn't find the amount." in bot.confirm_text(view, SETTINGS, SUNDAY)
    buttons = [b.text for row in bot.confirm_keyboard(RID, d).inline_keyboard for b in row]
    assert buttons == ["✏️ Fix"]


def test_reminders_say_who_added_them_and_how_late():
    view = ReminderView(RID, 222, draft(), owner_id=222, added_by=111, reason="reminder", now="2026-10-11T10:00:00+05:30")
    text = bot.reminder_text(view, SETTINGS)
    assert "Overdue by 2 days (was due Fri 9 Oct)" in text and "added by Prateek" in text
    view = ReminderView(RID, 222, draft(), 222, 222, "reminder", now="2026-10-08T19:00:00+05:30")
    assert "Due tomorrow, Fri 9 Oct · added by you" in bot.reminder_text(view, SETTINGS)


def test_admin_added_notice_states_the_plan_and_only_offers_paid():
    view = ReminderView(RID, 222, draft(title="Goa trip", payee="Priya", amount_inr=3450.0), 222, 111, "added",
                        now="2026-10-04T10:00:00+05:30", plan=["2026-10-08T19:00:00+05:30"])  # fmt: skip
    text = bot.reminder_text(view, SETTINGS)
    assert text.startswith("Prateek added a reminder for you:\nGoa trip · ₹3,450 to Priya")
    assert text.endswith("I'll remind you Thu 8 Oct 7 PM, then every morning until it's paid.")
    assert [b.text for row in bot.reminder_keyboard(RID, "added").inline_keyboard for b in row] == ["✅ Paid"]
    assert [b.text for row in bot.reminder_keyboard(RID, "last").inline_keyboard for b in row] == ["✅ Paid"]
    assert [b.text for row in bot.reminder_keyboard(RID).inline_keyboard for b in row] == ["✅ Paid", "😴 Snooze"]


def test_last_message_after_a_month():
    view = ReminderView(RID, 222, draft(), 222, 222, "last", now="2026-11-08T10:00:00+05:30")
    assert "I'll stop reminding you. It stays in /due" in bot.reminder_text(view, SETTINGS)


def test_plan_phrases():
    now = SUNDAY
    assert bot.plan_phrase(["2026-10-04T19:00:00+05:30", "2026-10-05T10:00:00+05:30"], None, now, IST) == (
        "today 7 PM, tomorrow 10 AM, then every morning until it's paid")
    demo = [(SUNDAY + timedelta(minutes=2 * k + 2)).isoformat() for k in range(5)]
    assert bot.plan_phrase(demo, 120, now, IST) == (
        "in 2 min, then every 2 min (5 times in all), then every morning until it's paid")


# --- prompts only count as replies ---


def test_a_reply_to_the_prompt_answers_it():
    data = {"awaiting": {"kind": "fix", "rid": RID, "prompt_id": 42}}
    assert bot.take_pending(data, reply_to=42, has_text=True)["kind"] == "fix"
    assert "awaiting" not in data


@pytest.mark.parametrize("reply_to, has_text", [(None, True), (41, True), (42, False)])
def test_any_other_message_cancels_the_prompt_and_is_read_normally(reply_to, has_text):
    data = {"awaiting": {"kind": "date", "rid": RID, "prompt_id": 42}}
    assert bot.take_pending(data, reply_to=reply_to, has_text=has_text) is None
    assert "awaiting" not in data  # a new IOU sent later is never taken as the old snooze date


def test_no_prompt_pending():
    assert bot.take_pending({}, reply_to=42, has_text=True) is None


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


def at(d: str, hh: int, mm: int = 0) -> datetime:
    return datetime.fromisoformat(f"{d}T{hh:02d}:{mm:02d}:00+05:30")


def walk(due, has_time, set_at, saved_at, n=8):
    """The first n reminders after saving, as the workflow would send them."""
    slots, out, after = plan(due, has_time, set_at, saved_at), [], saved_at
    while len(out) < n and (after := next_reminder(due, has_time, slots, after)):
        out.append(after)
    return out


def test_date_due_far_away_gets_an_early_notice():
    due = at("2026-10-09", 0)  # Fri
    assert walk(due, False, SUNDAY, SUNDAY, 6) == [
        at("2026-10-06", 10),  # 3 days before
        at("2026-10-08", 19),  # evening before
        at("2026-10-09", 10), at("2026-10-09", 19),  # due day + last call
        at("2026-10-10", 10), at("2026-10-11", 10),  # overdue, every morning
    ]


def test_no_early_notice_when_saved_less_than_3_days_ahead():
    due = at("2026-10-06", 0)  # Tue, saved Sun
    assert walk(due, False, SUNDAY, SUNDAY, 3) == [at("2026-10-05", 19), at("2026-10-06", 10), at("2026-10-06", 19)]


def test_saved_late_on_the_due_day_goes_straight_to_tomorrow_morning():
    due = at("2026-10-04", 0)
    late = at("2026-10-04", 20)
    assert walk(due, False, late, late, 2) == [at("2026-10-05", 10), at("2026-10-06", 10)]


def test_overdue_nudges_stop_after_30_days():
    due = at("2026-10-09", 0)
    reminders = walk(due, False, SUNDAY, SUNDAY, 100)
    assert reminders[-1] == at("2026-11-07", 10)  # due + 29 days, 10 AM
    assert len(reminders) == 4 + 29


def test_timed_due_hours_away_gets_30_minutes_notice_then_the_deadline():
    set_at = at("2026-10-04", 14)
    due = at("2026-10-04", 19)  # "in 5 hours"
    assert walk(due, True, set_at, set_at, 3) == [at("2026-10-04", 18, 30), due, at("2026-10-05", 10)]


def test_timed_due_under_an_hour_away_fires_once_at_the_deadline():
    set_at = at("2026-10-04", 14)
    due = at("2026-10-04", 14, 20)  # "in 20 minutes"
    assert walk(due, True, set_at, set_at, 2) == [due, at("2026-10-05", 10)]


def test_quiet_hours_move_the_early_reminder_earlier_and_the_deadline_later():
    set_at = at("2026-10-04", 20)
    due = at("2026-10-05", 1)  # "in 5 hours", lands at 1 AM
    assert walk(due, True, set_at, set_at, 3) == [at("2026-10-04", 21, 30), at("2026-10-05", 8), at("2026-10-05", 10)]


def test_demo_dues_repeat_5_times_from_when_they_were_set_then_go_daily():
    set_at = at("2026-10-04", 23)  # quiet hours don't apply to demos
    due = set_at + timedelta(minutes=2)
    assert demo_every(due, True, set_at) == timedelta(minutes=2)
    assert walk(due, True, set_at, set_at, 6) == [due + timedelta(minutes=2 * k) for k in range(5)] + [at("2026-10-05", 10)]


def test_demo_pace_is_measured_from_the_fix_not_the_first_message():
    fixed_at = at("2026-10-04", 10, 1)  # a Fix a minute after the message said "due in 2 minutes"
    due = fixed_at + timedelta(minutes=2)
    assert demo_every(due, True, fixed_at) == timedelta(minutes=2)
    assert demo_every(due, True, at("2026-10-04", 10)) == timedelta(minutes=3)


def test_ten_minutes_or_more_is_not_a_demo():
    set_at = at("2026-10-04", 14)
    assert demo_every(set_at + timedelta(minutes=10), True, set_at) is None
    assert demo_every(set_at + timedelta(days=2), False, set_at) is None
