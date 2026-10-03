import json
import stat
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from paidyet import extract
from paidyet.extract import (
    SCHEMA,
    Draft,
    ExtractionError,
    parse_amount,
    parse_relative,
    printed_date,
    review,
    validate,
)

IST = ZoneInfo("Asia/Kolkata")
SUNDAY = datetime(2026, 10, 4, 10, 0, tzinfo=IST)  # Sunday 4 Oct 2026
FRIDAY = datetime(2026, 10, 9, 10, 0, tzinfo=IST)


def raw(**kw):
    base = dict(kind="iou", title="Dinner split", payee="Rahul", amount_inr=860, due_evidence=None,
                due_date=None, due_relative=None, confidence=0.9)  # fmt: skip
    return base | kw


# --- relative and printed dates ----------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase, now, days, has_time",
    [
        ("Friday tak", SUNDAY, 5, False),
        ("friday", FRIDAY, 0, False),
        ("next Friday", FRIDAY, 7, False),
        ("shukravar tak", SUNDAY, 5, False),
        ("kal", SUNDAY, 1, False),
        ("by tomorrow", SUNDAY, 1, False),
        ("parso", SUNDAY, 2, False),
        ("aaj", SUNDAY, 0, False),
        ("3 din mein", SUNDAY, 3, False),
        ("in 1 week", SUNDAY, 7, False),
        ("month end", SUNDAY, 27, False),
    ],
)
def test_parse_relative_days(phrase, now, days, has_time):
    due, timed = parse_relative(phrase, now)
    assert timed is has_time
    assert due.date() == (now + timedelta(days=days)).date()
    assert due.tzinfo == IST


def test_parse_relative_demo_minutes_are_exact():
    due, timed = parse_relative("in 2 minutes", SUNDAY)
    assert timed and due == SUNDAY + timedelta(minutes=2)
    due, timed = parse_relative("2 ghante mein", SUNDAY)
    assert timed and due == SUNDAY + timedelta(hours=2)


@pytest.mark.parametrize("phrase", ["whenever", "soon bhai", ""])
def test_parse_relative_unknown(phrase):
    assert parse_relative(phrase, SUNDAY) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Pay by 12 Oct 2026", "2026-10-12"),
        ("Due date 09-10-2026", "2026-10-09"),  # day first
        ("Due by 10/10/26", "2026-10-10"),
        ("Renew by 20.10.2026", "2026-10-20"),
        ("9 Oct tak", "2026-10-09"),
        ("pay before Oct 9th", "2026-10-09"),
        ("2 Sep tak", "2027-09-02"),  # already passed this year, so next year
        ("due 2026-10-15", "2026-10-15"),
        ("31-02-2026", None),
        ("Friday tak", None),
        ("in 2 minutes", None),
    ],
)
def test_printed_date(text, expected):
    assert printed_date(text, SUNDAY) == expected


@pytest.mark.parametrize(
    "value, expected",
    [(860, 860.0), ("₹1,240.00", 1240.0), ("Rs. 500", 500.0), ("INR 99.999", 100.0),
     (None, None), (True, None), ("abc", None), (float("nan"), None)],
)  # fmt: skip
def test_parse_amount(value, expected):
    assert parse_amount(value) == expected


# --- validation: code, not the model, decides ---------------------------------------------------


def test_valid_iou():
    d = validate(raw(due_relative="Friday tak", due_evidence="Friday tak"), SUNDAY)
    assert d.ready and d.notes == []
    assert (d.kind, d.payee, d.amount_inr) == ("iou", "Rahul", 860.0)
    assert d.due_at == "2026-10-09T00:00:00+05:30" and not d.has_time


def test_quoted_evidence_beats_the_models_date():
    # Gemma once turned "Pay by 12 Oct 2026" into 2026-12-12.
    d = validate(raw(kind="bill", due_evidence="Pay by 12 Oct 2026", due_date="2026-12-12"), SUNDAY)
    assert d.due_at.startswith("2026-10-12")


def test_users_relative_phrase_beats_a_date():
    d = validate(raw(due_relative="in 2 minutes", due_date="2026-10-04"), SUNDAY)
    assert d.has_time and d.due_at == (SUNDAY + timedelta(minutes=2)).isoformat()


@pytest.mark.parametrize("evidence", ["Grace period ends 19-11-2026", "Bill date 24-09-2026",
                                      "Next invoice 02 Nov 2026", "Amount payable after due date 1,290"])  # fmt: skip
def test_non_deadline_labels_block_save(evidence):
    d = validate(raw(kind="bill", due_evidence=evidence, due_date="2026-11-19"), SUNDAY)
    assert not d.ready and "isn't a due date" in d.problems[0]
    assert d.due_at is None


def test_bill_without_due_date_blocks_save():
    d = validate(raw(kind="bill"), SUNDAY)
    assert d.problems == ["I couldn't find the due date on this bill."]


def test_iou_without_due_date_defaults_to_tomorrow_with_a_note():
    d = validate(raw(), SUNDAY)
    assert d.ready and d.due_at.startswith("2026-10-05")
    assert "tomorrow" in d.notes[0]


def test_past_due_dates_are_rejected():
    assert "already passed" in validate(raw(due_date="2026-10-03"), SUNDAY).problems[0]
    assert validate(raw(due_date="2026-10-04"), SUNDAY).ready  # due today is fine
    assert "already passed" in validate(raw(due_relative="in 0 minutes"), SUNDAY).problems[0]


@pytest.mark.parametrize("amount", [None, 0, -500, 2_000_000, "lots"])
def test_bad_amounts_block_save(amount):
    d = validate(raw(amount_inr=amount, due_relative="kal"), SUNDAY)
    assert not d.ready and d.amount_inr is None


def test_unparseable_due_date_blocks_save():
    d = validate(raw(due_date="someday"), SUNDAY)
    assert d.problems == ["I couldn't work out the due date."]


def test_low_confidence_is_a_note_not_a_blocker():
    d = validate(raw(due_relative="kal", confidence=0.2), SUNDAY)
    assert d.ready and any("not sure" in n for n in d.notes)


def test_messy_fields_are_cleaned():
    d = validate(raw(kind="loan", title="  ", payee="x" * 200, confidence="high", due_relative="kal"), SUNDAY)
    assert d.kind == "other" and d.title == "Payment" and len(d.payee) == 40 and d.confidence == 0.0


def test_review_hints():
    assert "not a payment deadline" in review(raw(kind="bill", due_evidence="Grace period ends 19-11-2026"), SUNDAY)
    assert "no due date" in review(raw(kind="bill"), SUNDAY)
    assert review(raw(kind="bill", due_evidence="Pay by 12 Oct 2026"), SUNDAY) is None
    assert review(raw(), SUNDAY) is None  # an IOU without a date is fine


# --- the Ollama call, with a fake transport -----------------------------------------------------


def fake_ollama(answers):
    """Replies with each answer in turn; records request bodies."""
    seen = []

    def handler(request: httpx.Request):
        seen.append(json.loads(request.content))
        content = answers[len(seen) - 1]
        return httpx.Response(200, json={
            "message": {"content": content if isinstance(content, str) else json.dumps(content)},
            "prompt_eval_count": 600, "eval_count": 80, "total_duration": 4_000_000_000, "load_duration": 0,
        })  # fmt: skip

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ollama"), seen


async def test_read_retries_once_with_a_hint_and_sends_the_image_both_times():
    first = raw(kind="bill", payee="Kavach", amount_inr=8450, due_evidence="Grace period ends 19-11-2026", due_date="2026-11-19")
    second = raw(kind="bill", payee="Kavach", amount_inr=8450, due_evidence="Renew by 20-10-2026", due_date="2026-10-20")
    http, seen = fake_ollama([first, second])
    async with http:
        draft, usages = await extract.read(http, "gemma4:e4b", SUNDAY, image=b"\x89PNG fake")
    assert draft.ready and draft.due_at.startswith("2026-10-20")
    assert len(usages) == 2 and usages[0].input_tokens == 600 and usages[0].total_ms == 4000
    assert all(b["format"] == SCHEMA and b["think"] is False and b["stream"] is False for b in seen)
    assert all(b["messages"][1]["images"] for b in seen)
    assert "Check your answer" not in seen[0]["messages"][1]["content"]
    assert "Grace period ends" in seen[1]["messages"][1]["content"]


async def test_read_does_not_retry_a_good_answer():
    http, seen = fake_ollama([raw(due_relative="Friday tak")])
    async with http:
        draft, usages = await extract.read(http, "gemma4:e4b", SUNDAY, text="Rahul ko 860 dene hai Friday tak")
    assert draft.ready and len(seen) == 1 and "images" not in seen[0]["messages"][1]
    assert "Rahul ko 860" in seen[0]["messages"][1]["content"]


async def test_fix_sends_previous_fields_and_correction_but_no_image():
    previous = Draft(kind="bill", title="Electricity", payee="Sahyadri", amount_inr=1290.0,
                     due_at="2026-10-09T00:00:00+05:30", has_time=False, confidence=0.9)  # fmt: skip
    http, seen = fake_ollama([raw(kind="bill", amount_inr=1240, due_date="2026-10-09")])
    async with http:
        draft, _ = await extract.read(http, "gemma4:e4b", SUNDAY, previous=previous, correction="amount is 1240")
    msg = seen[0]["messages"][1]
    assert "images" not in msg
    assert "amount is 1240" in msg["content"] and '"amount_inr": 1290.0' in msg["content"]
    assert draft.amount_inr == 1240.0


async def test_unusable_reply_raises_a_retryable_error():
    http, _ = fake_ollama(["not json at all"])
    async with http:
        with pytest.raises(ExtractionError):
            await extract.ask_gemma(http, "gemma4:e4b", SUNDAY, text="x")


# --- photos: on disk only while being read ------------------------------------------------------


def test_save_photo_uses_a_random_private_name(tmp_path):
    a = extract.save_photo(tmp_path, b"one", ".jpg")
    b = extract.save_photo(tmp_path, b"two", ".jpg")
    assert a != b and extract.PHOTO_NAME.match(a)
    path = extract.photo_path(tmp_path, a)
    assert path.read_bytes() == b"one"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_discard_photo_deletes_it(tmp_path):
    name = extract.save_photo(tmp_path, b"bill", ".png")
    assert extract.discard_photo(tmp_path, name) is True
    assert list(tmp_path.iterdir()) == []
    assert extract.discard_photo(tmp_path, name) is False  # idempotent, safe to retry


@pytest.mark.parametrize("name", ["../secret.png", "/etc/passwd", "abc.png", "0" * 32 + ".gif", "0" * 32 + ".png/x"])
def test_photo_path_refuses_anything_but_our_names(tmp_path, name):
    with pytest.raises(ValueError):
        extract.photo_path(tmp_path, name)


def test_save_photo_refuses_other_types(tmp_path):
    with pytest.raises(ValueError):
        extract.save_photo(tmp_path, b"%PDF", ".pdf")


def test_sweep_deletes_leftovers(tmp_path):
    for i in range(3):
        extract.save_photo(tmp_path, b"x", ".jpg")
    assert extract.sweep(tmp_path) == 3
    assert list(tmp_path.iterdir()) == []
    assert extract.sweep(tmp_path / "missing") == 0
