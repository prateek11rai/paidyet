"""Read a bill or IOU with local Gemma (Ollama structured output), then validate it with plain code.

The model only fills in fields. Dates, amounts and "is this ready to save" are decided here, in code.
"""

import base64
import calendar
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

import httpx
import sentry_sdk
from sentry_sdk.ai.monitoring import record_token_usage
from sentry_sdk.consts import OP, SPANDATA

KINDS = ("bill", "iou", "other")
MAX_AMOUNT_INR = 1_000_000
LOW_CONFIDENCE = 0.5
PHOTO_SUFFIXES = (".jpg", ".png", ".webp")
PHOTO_NAME = re.compile(r"^[0-9a-f]{32}\.(jpg|png|webp)$")

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "title": {"type": "string"},
        "payee": {"type": ["string", "null"]},
        "amount_inr": {"type": ["number", "null"]},
        "due_evidence": {"type": ["string", "null"]},
        "due_date": {"type": ["string", "null"]},
        "due_relative": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
    },
    "required": ["kind", "title", "payee", "amount_inr", "due_evidence", "due_date", "due_relative", "confidence"],
}

SYSTEM_PROMPT = """\
You read bills, payment screenshots and short notes for a user in India, and pull out the one payment they need to make.
Notes are often Hinglish, e.g. "Rahul ko 500 dene hai Friday tak" means "I have to give Rahul 500 by Friday".
Reply with JSON only, using these fields:
- kind: "bill" (electricity, broadband, rent, insurance, subscriptions), "iou" (money owed to a person), or "other".
- title: 1 to 3 words for what the payment is for, e.g. "Electricity", "Broadband", "Dinner split", "Goa trip".
- payee: who receives the money, as a name. The user is always the one paying, so the payee is never the user or the
  person the message is addressed to. For an IOU it is the person asking for or owed the money (in a forwarded
  message, usually its sender). For a bill it is the company, or the landlord's name (e.g. "R. Menon"), never a
  label like "Landlord". null if unknown.
- amount_inr: the amount to pay now, in rupees, as a number. For a bill this is the total payable by the due date,
  not a line item, a late-payment amount, a deposit or a sum insured. null if no amount is given.
- due_evidence: the exact words around the deadline, copied with their label, e.g. "Pay by 12 Oct 2026" or
  "Friday tak". null if there is no deadline.
- due_date: the payment deadline as YYYY-MM-DD, only if a calendar date is written. Indian dates are day first
  (09-10-2026 is 9 October 2026). Use the date labelled due date, "pay by", "renew by" or "last date". Never use the
  bill date, invoice date, statement date, billing period, next invoice date or the end of a grace period. null otherwise.
- due_relative: if the deadline is relative ("Friday", "kal", "tomorrow", "in 2 minutes", "3 din mein"),
  copy that phrase as written. null otherwise.
- confidence: 0 to 1, how sure you are about the amount and the deadline."""


class ExtractionError(Exception):
    """Gemma's reply wasn't usable JSON. Worth retrying."""


@dataclass
class Usage:
    model: str
    input_tokens: int
    output_tokens: int
    total_ms: int
    load_ms: int
    prompt_ms: int = 0
    eval_ms: int = 0


@dataclass
class Draft:
    """Validated fields only: no image, no raw model text."""

    kind: str
    title: str
    payee: str | None
    amount_inr: float | None
    due_at: str | None  # ISO 8601 in the user's time zone
    has_time: bool  # True for "in 2 minutes"; False means "due on that date"
    confidence: float
    problems: list[str] = field(default_factory=list)  # block Save; the user taps Fix
    notes: list[str] = field(default_factory=list)  # shown, but Save still allowed

    @property
    def ready(self) -> bool:
        return not self.problems

    def fields_for_model(self) -> dict:
        """What Gemma sees when the user asks to fix this draft."""
        d = asdict(self)
        return {k: d[k] for k in ("kind", "title", "payee", "amount_inr", "due_at")}


# --- Calling Gemma -----------------------------------------------------------------------------


def _user_message(
    now: datetime, text: str | None, image: bytes | None, previous: Draft | None, correction: str | None, hint: str | None
) -> dict:
    lines = [f"Today is {now:%A %-d %B %Y}."]
    if previous is not None:
        lines += [
            f"Earlier you read this payment as: {json.dumps(previous.fields_for_model(), ensure_ascii=False)}",
            f"The user says: {correction}",
            "Apply the user's correction. Keep every field the user did not correct.",
        ]
    elif image is not None:
        lines.append("Read the payment in this image.")
        if text:
            lines.append(f"The user's caption: {text}")
    else:
        lines.append(f"The user's message: {text}")
    if hint:
        lines.append(f"Check your answer: {hint}")
    msg = {"role": "user", "content": "\n".join(lines)}
    if image is not None and previous is None:
        msg["images"] = [base64.b64encode(image).decode()]
    return msg


async def ask_gemma(
    http: httpx.AsyncClient,
    model: str,
    now: datetime,
    *,
    text: str | None = None,
    image: bytes | None = None,
    previous: Draft | None = None,
    correction: str | None = None,
    hint: str | None = None,
    think: bool = False,
) -> tuple[dict, Usage]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            _user_message(now, text, image, previous, correction, hint),
        ],
        "format": SCHEMA,
        "stream": False,
        "think": think,
        "options": {"temperature": 0},
    }
    # The span records the model, tokens and Ollama's timings. Never the prompt, the image or the reply.
    with sentry_sdk.start_span(op=OP.GEN_AI_CHAT, name=f"chat {model}") as span:
        span.set_data(SPANDATA.GEN_AI_OPERATION_NAME, "chat")
        span.set_data(SPANDATA.GEN_AI_SYSTEM, "ollama")
        span.set_data(SPANDATA.GEN_AI_REQUEST_MODEL, model)
        span.set_data(SPANDATA.GEN_AI_REQUEST_TEMPERATURE, 0)
        span.set_data("paidyet.input", "fix" if previous else "photo" if image else "text")
        span.set_data("paidyet.hinted", hint is not None)
        r = await http.post("/api/chat", json=body)
        r.raise_for_status()
        data = r.json()
        usage = Usage(
            model=model,
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
            total_ms=data.get("total_duration", 0) // 1_000_000,
            load_ms=data.get("load_duration", 0) // 1_000_000,
            prompt_ms=data.get("prompt_eval_duration", 0) // 1_000_000,
            eval_ms=data.get("eval_duration", 0) // 1_000_000,
        )
        span.set_data(SPANDATA.GEN_AI_RESPONSE_MODEL, data.get("model", model))
        record_token_usage(span, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
        span.set_data("ollama.load_ms", usage.load_ms)
        span.set_data("ollama.prompt_eval_ms", usage.prompt_ms)
        span.set_data("ollama.eval_ms", usage.eval_ms)
        try:
            raw = json.loads(data["message"]["content"])
        except (KeyError, json.JSONDecodeError) as e:
            span.set_status("internal_error")
            raise ExtractionError("Gemma's reply was not valid JSON") from e
        if not isinstance(raw, dict):
            span.set_status("internal_error")
            raise ExtractionError("Gemma's reply was not a JSON object")
    return raw, usage


async def read(
    http: httpx.AsyncClient,
    model: str,
    now: datetime,
    *,
    text: str | None = None,
    image: bytes | None = None,
    previous: Draft | None = None,
    correction: str | None = None,
) -> tuple[Draft, list[Usage]]:
    """Ask Gemma; if code spots a known mistake, ask once more with a pointed hint; then validate."""
    with sentry_sdk.start_span(op=OP.GEN_AI_INVOKE_AGENT, name="invoke_agent paidyet-reader") as agent:
        agent.set_data(SPANDATA.GEN_AI_OPERATION_NAME, "invoke_agent")
        agent.set_data(SPANDATA.GEN_AI_AGENT_NAME, "paidyet-reader")
        agent.set_data(SPANDATA.GEN_AI_SYSTEM, "ollama")
        agent.set_data(SPANDATA.GEN_AI_REQUEST_MODEL, model)
        agent.set_data("paidyet.input", "fix" if previous else "photo" if image else "text")

        raw, usage = await ask_gemma(http, model, now, text=text, image=image, previous=previous, correction=correction)
        usages = [usage]
        with _tool_span("review_answer", "Code checks Gemma's answer for mistakes it knows about") as tool:
            hint = review(raw, now)
            tool.set_data("paidyet.retry_reason", _hint_kind(hint))
        if hint:
            raw, usage = await ask_gemma(
                http, model, now, text=text, image=image, previous=previous, correction=correction, hint=hint
            )
            usages.append(usage)
        with _tool_span("validate_draft", "Code decides the dates, the amount and whether Save is allowed") as tool:
            draft = validate(raw, now)
            tool.set_data("paidyet.ready", draft.ready)
            tool.set_data("paidyet.problems", len(draft.problems))

        agent.set_data("paidyet.model_calls", len(usages))
        agent.set_data("paidyet.retry_reason", _hint_kind(hint))
        agent.set_data("paidyet.ready", draft.ready)
        record_token_usage(
            agent,
            input_tokens=sum(u.input_tokens for u in usages),
            output_tokens=sum(u.output_tokens for u in usages),
        )
    return draft, usages


def _tool_span(name: str, description: str):
    span = sentry_sdk.start_span(op=OP.GEN_AI_EXECUTE_TOOL, name=f"execute_tool {name}")
    span.set_data(SPANDATA.GEN_AI_OPERATION_NAME, "execute_tool")
    span.set_data(SPANDATA.GEN_AI_TOOL_NAME, name)
    span.set_data(SPANDATA.GEN_AI_TOOL_DESCRIPTION, description)
    span.set_data("gen_ai.tool.type", "function")
    return span


def _hint_kind(hint: str | None) -> str:
    """A category for traces; the hint text itself can quote the bill."""
    if hint is None:
        return "none"
    return "wrong_date_label" if "not a payment deadline" in hint else "no_due_date"


# --- Deterministic validation

_NOT_A_DEADLINE = re.compile(
    r"\b(bill|invoice|statement|notice|issue)\s+date\b|\bnext\s+(invoice|bill|reading)\b|\bmeter reading\b"
    r"|\bgrace\b|\bperiod\b|\bafter\b",
    re.I,
)
_DEADLINE_LABELS = "'Due date', 'Pay by', 'Due by', 'Renew by' or 'Last date'"
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m} | {
    m.lower(): i for i, m in enumerate(calendar.month_name) if m
}
_NUMERIC_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b|\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})\b")
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\.?,?(?:\s+(\d{4}))?\b", re.I)
_MONTH_DAY = re.compile(r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?(?:\s+(\d{4}))?\b", re.I)


def printed_date(text: str, now: datetime) -> str | None:
    """Parse the date in quoted text like "Pay by 12 Oct 2026" or "Due date 09-10-2026" (day first). ISO or None."""

    def make(y, m, d):
        try:
            if y is None:  # "9 Oct": the next 9 Oct from today
                candidate = date(now.year, m, d)
                return candidate if candidate >= now.date() else date(now.year + 1, m, d)
            return date(y + 2000 if y < 100 else y, m, d)
        except ValueError:
            return None

    found = None
    if m := _NUMERIC_DATE.search(text):
        found = make(int(m[1]), int(m[2]), int(m[3])) if m[1] else make(int(m[6]), int(m[5]), int(m[4]))
    elif (m := _DAY_MONTH.search(text)) and m[2].lower() in _MONTHS:
        found = make(int(m[3]) if m[3] else None, _MONTHS[m[2].lower()], int(m[1]))
    elif (m := _MONTH_DAY.search(text)) and m[1].lower() in _MONTHS:
        found = make(int(m[3]) if m[3] else None, _MONTHS[m[1].lower()], int(m[2]))
    return found.isoformat() if found else None


def wrong_deadline_label(raw: dict) -> str | None:
    """The quoted words around the deadline, if they say it isn't a payment deadline."""
    evidence = raw.get("due_evidence")
    if isinstance(evidence, str) and _NOT_A_DEADLINE.search(evidence):
        return evidence.strip()
    return None


_LABEL_KINDS = (
    ("grace", "the end of a grace period"),
    ("next", "the next bill's date"),
    ("reading", "a meter-reading date"),
    ("period", "a billing period"),
    ("after", "the late-fee date"),
)


def _label_kind(label: str) -> str:
    """Name the kind of date without quoting the bill: problems end up in Temporal history."""
    lowered = label.lower()
    return next((kind for word, kind in _LABEL_KINDS if word in lowered), "the bill date")


def choose_date(raw: dict, now: datetime) -> tuple[str | None, str | None]:
    """(calendar due date, problem). Code parses the quoted evidence; the model's own date is only a fallback."""
    if label := wrong_deadline_label(raw):
        return None, f"The date I found looks like {_label_kind(label)}, not the due date."
    evidence = raw.get("due_evidence")
    if isinstance(evidence, str) and (parsed := printed_date(evidence, now)):
        return parsed, None
    return _clean(raw.get("due_date")), None


def review(raw: dict, now: datetime) -> str | None:
    """A hint for a second try when the first answer has a mistake code can see. None if it looks fine."""
    due_date, problem = choose_date(raw, now)
    if problem:
        label = wrong_deadline_label(raw)
        return f'you took the date from "{label}", which is not a payment deadline. Use the date labelled {_DEADLINE_LABELS}.'
    if raw.get("kind") == "bill" and not due_date and not raw.get("due_relative"):
        return f"you found no due date, but bills almost always print one, labelled {_DEADLINE_LABELS}. Look again."
    return None


_WEEKDAYS = {
    **{name: i for i, name in enumerate(calendar.day_name)},
    **{name[:3]: i for i, name in enumerate(calendar.day_name)},
    "somvar": 0, "mangalvar": 1, "budhvar": 2, "guruvar": 3, "brihaspativar": 3,
    "shukravar": 4, "shanivar": 5, "ravivar": 6, "itvaar": 6, "itwar": 6,
}  # fmt: skip
_WEEKDAYS = {k.lower(): v for k, v in _WEEKDAYS.items()}
_UNITS = {
    "minute": "minutes", "minutes": "minutes", "min": "minutes", "mins": "minutes",
    "hour": "hours", "hours": "hours", "hr": "hours", "hrs": "hours", "ghanta": "hours", "ghante": "hours",
    "day": "days", "days": "days", "din": "days",
    "week": "weeks", "weeks": "weeks", "hafta": "weeks", "hafte": "weeks",
}  # fmt: skip
_IN_N = re.compile(r"\b(\d{1,3})\s*(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")\b")


def parse_relative(phrase: str, now: datetime) -> tuple[datetime, bool] | None:
    """`"Friday tak"`, `"kal"`, `"in 2 minutes"`, `"3 din mein"` -> (due, has_time). None if not understood."""
    p = phrase.lower()
    today = now.date()
    if m := _IN_N.search(p):
        n, unit = int(m.group(1)), _UNITS[m.group(2)]
        if unit in ("minutes", "hours"):
            return now + timedelta(**{unit: n}), True
        return _day(today + timedelta(**{unit: n}), now), False
    if re.search(r"\b(day after tomorrow|parso|parson)\b", p):
        return _day(today + timedelta(days=2), now), False
    if re.search(r"\b(tomorrow|tmrw|kal)\b", p):  # "kal" in a deadline means tomorrow
        return _day(today + timedelta(days=1), now), False
    if re.search(r"\b(today|tonight|aaj)\b", p):
        return _day(today, now), False
    if re.search(r"\b(month end|end of (the )?month|mahine ke end)\b", p):
        return _day(today.replace(day=calendar.monthrange(today.year, today.month)[1]), now), False
    for word in re.findall(r"[a-z]+", p):
        if word in _WEEKDAYS:
            ahead = (_WEEKDAYS[word] - today.weekday()) % 7
            if ahead == 0 and "next" in p:
                ahead = 7
            return _day(today + timedelta(days=ahead), now), False
    return None


def _day(d: date, now: datetime) -> datetime:
    return datetime.combine(d, time(0, 0), tzinfo=now.tzinfo)


def parse_due(due_relative: str | None, due_date: str | None, now: datetime) -> tuple[datetime, bool] | None:
    """Prefer the user's own relative phrase (parsed here); fall back to the model's calendar date."""
    if due_relative and (parsed := parse_relative(due_relative, now)):
        return parsed
    if due_date:
        try:
            return _day(date.fromisoformat(due_date.strip()), now), False
        except ValueError:
            return None
    return None


def parse_amount(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = re.sub(r"[₹,\s]|rs\.?|inr", "", value, flags=re.I)
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or value != value:  # NaN
        return None
    return round(float(value), 2)


def _clean(value, limit: int = 40) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())[:limit].strip()
    return value or None


def validate(raw: dict, now: datetime) -> Draft:
    problems: list[str] = []
    notes: list[str] = []

    amount = parse_amount(raw.get("amount_inr"))
    if amount is None:
        problems.append("I couldn't find the amount.")
    elif not 0 < amount <= MAX_AMOUNT_INR:
        problems.append(f"₹{amount:,.0f} doesn't look like a real amount.")
        amount = None

    kind = raw.get("kind") if raw.get("kind") in KINDS else "other"
    due_relative = _clean(raw.get("due_relative"))
    due_date, date_problem = choose_date(raw, now)
    due = parse_due(due_relative, due_date, now)
    if due is None and date_problem:
        problems.append(date_problem)
    elif due is None and (due_relative or due_date):
        problems.append("I couldn't work out the due date.")
    elif due is None and kind == "bill":
        problems.append("I couldn't find the due date on this bill.")
    elif due is None:
        due = (_day(now.date() + timedelta(days=1), now), False)
        notes.append("No due date was given, so I set it to tomorrow.")
    if due is not None:
        due_at, has_time = due
        if (has_time and due_at <= now) or (not has_time and due_at.date() < now.date()):
            problems.append("That due date has already passed.")

    try:
        confidence = min(max(float(raw.get("confidence") or 0), 0.0), 1.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < LOW_CONFIDENCE:
        notes.append("I'm not sure I read this right, so please check it.")

    return Draft(
        kind=kind,
        title=_clean(raw.get("title"), 30) or "Payment",
        payee=_clean(raw.get("payee")),
        amount_inr=amount,
        due_at=due[0].isoformat() if due else None,
        has_time=due[1] if due else False,
        confidence=round(confidence, 2),
        problems=problems,
        notes=notes,
    )


# --- Inputs live outside Temporal: texts in memory, photos on disk while being read --------------

_texts: dict[str, str] = {}


def remember_text(text: str) -> str:
    """Keep a message in memory only; workflows carry the returned key, never the text."""
    key = secrets.token_hex(16)
    _texts[key] = text
    return key


def recall_text(key: str) -> str | None:
    return _texts.get(key)


def forget_text(key: str) -> None:
    _texts.pop(key, None)


def save_photo(tmp_dir: Path, data: bytes, suffix: str) -> str:
    """Write an upload under a random name (mode 0600) and return just the name."""
    if suffix not in PHOTO_SUFFIXES:
        raise ValueError(f"unsupported image type {suffix!r}")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_hex(16)}{suffix}"
    fd = os.open(tmp_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return name


def photo_path(tmp_dir: Path, name: str) -> Path:
    """Resolve a name from save_photo; anything else (paths, traversal) is refused."""
    if not PHOTO_NAME.match(name):
        raise ValueError("not a PaidYet photo name")
    return tmp_dir / name


def discard_photo(tmp_dir: Path, name: str) -> bool:
    path = photo_path(tmp_dir, name)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def sweep(tmp_dir: Path) -> int:
    """Delete anything a crash left behind. Called on start."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    leftovers = [p for p in tmp_dir.iterdir() if p.is_file()]
    for p in leftovers:
        p.unlink()
    return len(leftovers)
