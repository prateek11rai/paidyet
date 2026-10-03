<p align="center"><img src="docs/img/logo.svg" alt="PaidYet logo" width="120"></p>

# PaidYet

**Keeps reminding your friend until they've actually paid.**

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](.python-version)

PaidYet is a Telegram bot that runs on your laptop, for the friend who always says "haan bhai, kal bhej dunga" ("yeah, I'll send it tomorrow") and then forgets. Send it a bill photo or a line like "Rahul ko 500 dene hai Friday tak". [Gemma](https://ai.google.dev/gemma), running locally in [Ollama](https://ollama.com), reads it, and you tap Save. Then a durable [Temporal](https://temporal.io) workflow keeps reminding you until you tap Paid. [Sentry](https://sentry.io) traces every step without seeing a single bill.

## What it is

A private reminder bot for money you owe: your share of a dinner, a trip someone else paid for, a friend's loan, the flat's electricity and broadband. Reading a bill happens on your machine. The reminders survive the laptop sleeping, restarting or crashing. An admin (you, if you run it for friends) can add dues for a friend, and every reminder says who added it.

How it works:

1. **Capture.** Send a bill photo or screenshot, forward a message, or type a line in English or Hinglish.
2. **Gemma reads it.** It fills in title, payee, amount, due date and kind. Plain code then checks the answer: it parses the dates, rejects past due dates and odd amounts, and refuses to save a bill whose date it can't trust.
3. **Confirm.** "Electricity · ₹1,240 · due Fri 9 Oct", then the whole plan ("I'll remind you Tue 6 Oct 10 AM, Thu 8 Oct 7 PM, Fri 9 Oct 10 AM and 7 PM, then every morning until it's paid"), with **✅ Save** and **✏️ Fix**.
4. **Temporal reminds.** On the schedule below, durably, until Paid.
5. **Paid or Snooze.** **✅ Paid** closes it. **😴 Snooze** offers Tomorrow, In 3 days, or a date picker, and it only ever postpones.

When reminders go out:

| Due | Reminders |
|---|---|
| A date ("Friday tak", a bill due 12 Oct) | 10 AM three days before (if it's at least 3 days away), 7 PM the evening before, 10 AM and 7 PM on the day, then 10 AM daily while overdue |
| A time at least 1 hour away ("in 5 hours") | 30 minutes before and at the deadline, then 10 AM daily |
| A time 10 minutes to 1 hour away | At the deadline, then 10 AM daily |
| Under 10 minutes (demo mode, "in 2 minutes") | At the deadline and every 2 minutes after, 5 times in all, then 10 AM daily |

Nothing goes out between 22:00 and 08:00, except demo reminders. A reminder that has to beat the deadline moves earlier, to 21:30; anything else waits until 08:00. After 30 days overdue, PaidYet sends one last message, and the due then waits quietly in `/due` until it's paid.

## Built for

The DEV [Hacktoberfest Weekend Challenge: Build for a Friend](https://dev.to/challenges/hacktoberfest-weekend-2026-10-01), part of Hacktoberfest 2026 (MLH × DEV). Submission post: [PaidYet: for the friend who says “kal bhej dunga”](https://dev.to/prateek11rai/paidyet-for-the-friend-who-says-kal-bhej-dunga-p5l).

Prize categories entered: **Best Use of Gemma**, **Best Use of Temporal** and **Best Use of Sentry Agent Tracing**.

## Requirements

- macOS or Linux, [Homebrew](https://brew.sh), [uv](https://docs.astral.sh/uv/), Python 3.12 and a Telegram account.

> [!NOTE]
> Gemma runs locally, so it needs real memory and, ideally, a GPU. See [Gemma's docs](https://ai.google.dev/gemma) and the [Ollama model page](https://ollama.com/library/gemma4) for system requirements. We built and tested on an Apple M4 with 16 GB using `gemma4:e4b`: about 3 s to read a text and about 7 s for a photo once the model is loaded. On a smaller machine, set `OLLAMA_MODEL=gemma4:e2b`.
>
> PaidYet keeps Gemma loaded for 30 minutes after each read (Ollama's `keep_alive`), so a reply doesn't wait ~20 s for the model to load. The cost is about 6.6 GB of RAM held for those 30 minutes. To free it sooner, lower `OLLAMA_KEEP_ALIVE` in `paidyet/extract.py`.

## Setup

1. Install the tools: `brew install ollama temporal uv`
2. Pull Gemma: `ollama pull gemma4:e4b`. (`poe up` also pulls it if it's missing.)
3. Clone and install:
   ```sh
   git clone https://github.com/prateek11rai/paidyet && cd paidyet
   uv sync
   ```
   uv uses a Python 3.12 that's already on your machine (we use pyenv) and never downloads one.

   > [!NOTE]
   > If your uv is configured with a private package mirror as its default index, `uv sync` and plain `uv run` re-lock `uv.lock` against it. Run commands as `uv run --frozen …` there, and don't commit the re-locked file; a unit test catches it if you do.
4. Configure: `cp .env.example .env`, then fill in the values.

| Variable | What it's for | Where to get it |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot | In Telegram, message [@BotFather](https://t.me/BotFather) and send `/newbot` |
| `SENTRY_DSN` | Tracing (optional) | Sentry → your Python project → Settings → Client Keys (DSN) |
| `ADMIN_USER_ID` | You. You can add reminders for friends | Send `/start` to your bot; it replies with your Telegram ID |
| `ALLOWED_USERS` | Who may use the bot, as `name:id` pairs, e.g. `me:111,arjun:222` | Each friend sends `/start` and tells you their ID |
| `OLLAMA_MODEL` | The Gemma model | `gemma4:e4b` (default) or `gemma4:e2b` |
| `OLLAMA_HOST` | Where Ollama listens | Leave at `127.0.0.1:11434`. Anything other than loopback is refused |
| `PAIDYET_DATA_DIR` | SQLite, Temporal history, temporary photos, logs | Default `.data` (gitignored) |
| `TZ` | Time zone for due dates | Default `Asia/Kolkata` |

PaidYet starts without a bot token or a Sentry DSN. It logs that it skipped them.

## Run

```sh
uv run poe up
```

This starts Ollama (on 127.0.0.1 only) and the Temporal dev server (UI at <http://127.0.0.1:8233>), then runs the bot and the Temporal worker in the foreground. **Ctrl-C** stops the bot and both servers. Only processes `poe up` started are stopped; if Ollama was already running, it's reused and left alone.

| Task | What it does |
|---|---|
| `uv run poe up` | Start everything; Ctrl-C stops everything |
| `uv run poe run` | The app only, when Ollama and Temporal are already up |
| `uv run poe start-deps` / `stop-deps` | Start or stop Ollama and Temporal only (stopped by pid file, never by port) |
| `uv run poe test` | Unit tests, no services needed |
| `uv run poe test-integration` | Integration tests against a running `poe up` |

> [!NOTE]
> Can the laptop sleep? Yes. Telegram holds incoming messages for 24 hours, and Temporal fires any timers it missed when it wakes up. A reminder can be late, but it is never lost.

## Use it

| Send | What happens |
|---|---|
| A bill photo or screenshot | "👀 Reading it…", then the confirm message with ✅ Save / ✏️ Fix |
| `Rahul ko 500 dene hai Friday tak` | An IOU to Rahul for ₹500, due this Friday |
| A forwarded message, e.g. "Goa trip ka tera share 3,450 hua, Saturday tak bhej dena" | The sender becomes the payee |
| `Neha ko 200 dene hai in 2 minutes` | Demo mode: reminds you in 2 minutes, then every 2 minutes, 5 times in all |
| ✏️ Fix, then reply `amount is 1340` or `due 12 Oct` | Gemma applies the correction; the confirm message updates. Only a reply to the bot's question counts, so a new IOU is never mistaken for a correction |
| `/cancel` | Drops a pending Fix or date question |
| ✅ Paid | The reminder becomes "✅ Paid on Sun 4 Oct: …" and stops |
| 😴 Snooze | Tomorrow · In 3 days · 📅 Pick date (tap a day, or reply `12 Oct`). A time before the next scheduled reminder is refused |
| `/due` | Overdue and upcoming dues, each "added by you" or "added by <admin>", with a Paid button |
| `/remind arjun ₹500 to Rahul by Fri` (admin) | Adds it to Arjun's list. Arjun gets "<admin> added a reminder for you" with the plan and a Paid button, and you're told when he pays |
| A photo captioned `for arjun` (admin) | The same, from a bill photo |

Anyone not on the allowlist gets "This is a private bot. Your Telegram ID is N; send it to the owner." No model call is made for them.

> [!WARNING]
> PDFs aren't supported yet. Send a screenshot of the bill.

## Repository map

| Path | Purpose |
|---|---|
| `main.py` | Loads `.env`, starts Sentry, runs the Temporal worker and the Telegram bot together |
| `paidyet/config.py` | Typed settings from the environment |
| `paidyet/extract.py` | The Gemma call (Ollama structured output), deterministic validation, photo and text lifecycle |
| `paidyet/workflows.py` | `ReminderWorkflow`: timers, signals, the status query; no I/O |
| `paidyet/activities.py` | Side effects: Gemma, Telegram, SQLite |
| `paidyet/bot.py` | Telegram handlers, buttons, the snooze calendar, the allowlist, message text |
| `paidyet/store.py` | SQLite read model for `/due` |
| `tests/unit/` | Validation, parsing, photo deletion, workflow logic in Temporal's time-skipping server, Sentry privacy |
| `tests/integration/` | The real stack: Gemma on the samples, full reminder cycles, photo deletion |
| `samples/` | Synthetic bills and IOU messages (fake names and numbers) with expected answers |
| `docs/` | `BUILD_LOG.md` (decisions, dead ends, real numbers), the DEV post, images |

## Testing

```sh
uv run poe test               # 137 unit tests, ~6 s, no services needed
uv run poe test-integration   # with `uv run poe up` running in another terminal
```

The unit tests run the workflow in Temporal's time-skipping test server, so a week of reminders takes milliseconds. Its binary downloads once into `.pytest_cache/temporal/`. The integration tests use real Gemma, Temporal and Telegram, but message only `ADMIN_USER_ID`, prefixed `[test]`. They use their own task queue and SQLite file. If the stack isn't up, they fail with "Run `uv run poe up` first"; they never skip.

## Privacy

- **Photos.** A photo is written to `.data/tmp/` under a random name, readable only by you, and deleted as soon as Gemma has read it. That happens on success and after the final failed retry. `.data/tmp/` is also swept on every start.
- **Text.** What you type is held in memory only until it's read. Temporal's history (`.data/temporal.db`) gets a random key, never the text, the image or Gemma's raw reply.
- **What's stored.** Only the fields you confirmed (title, payee, amount, due date, kind) plus Telegram chat and message IDs, in SQLite and Temporal.
- **Sentry.** Sentry gets model names, token counts and timings. It never gets prompts, replies, amounts, names or images: `send_default_pii=False`, no local variables, the httpx integration off (Telegram URLs contain the bot token), and a scrubber for anything token-shaped. A unit test sends a read through Sentry's real configuration and checks nothing leaks.
- **Ollama** listens only on 127.0.0.1.

> [!WARNING]
> Telegram bot chats are **not** end-to-end encrypted. The photo you send also stays in your Telegram chat, on Telegram's servers. What PaidYet promises is narrower: no AI company reads your bill, keeps it, or bills you for it.

## How it's built

**Gemma** (gemma4:e4b in Ollama) reads photos and Hinglish through Ollama's JSON-schema structured output, at temperature 0 with thinking off. It's asked to *quote* the words around the deadline ("Pay by 12 Oct 2026"), and code parses the date out of that quote. When code spots a known mistake, such as a bill with no due date or a date labelled "grace period", it asks Gemma once more with a pointed hint, and a draft that's still wrong can't be saved. On our samples that means no silently wrong due dates. The numbers and dead ends are in [docs/BUILD_LOG.md](docs/BUILD_LOG.md).

**Temporal** runs one `ReminderWorkflow` per due, from the moment a message arrives. Reading the bill is a retried activity, so a slow model load or bad JSON just gets another try. The input is deleted after the read, on success or final failure. Then the workflow waits durably for Save or Fix, sends reminders on timers, and reacts to Paid and Snooze signals. Its SQLite writes get five tries and then report the failure, while Telegram sends keep retrying through an offline laptop. A long-overdue due continues-as-new, so its history stays short. Killing the worker mid-wait and restarting it loses nothing.

**Sentry Agent Tracing** shows each read as an `invoke_agent` span. Inside it are a `gen_ai.chat` span per Gemma call (tokens, plus how Ollama's time split between loading the model, reading the prompt and generating) and `execute_tool` spans for the code checks. Temporal interceptors keep one trace from the Telegram update through the workflow into every activity, including a reminder sent days later.

## License and attribution

- Code: [MIT](LICENSE).
- Logo: Lucide's [`badge-indian-rupee`](https://lucide.dev/icons/badge-indian-rupee) (ISC), recoloured. See [docs/img/logo-LICENSE.txt](docs/img/logo-LICENSE.txt).
- Gemma: `gemma4` is used under its own terms. The model ships with the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0), included in the [Ollama model](https://ollama.com/library/gemma4).

## After the deadline

The challenge closed on Mon 5 Oct 2026, 06:59 UTC. Commits after that will be listed here. None yet.
