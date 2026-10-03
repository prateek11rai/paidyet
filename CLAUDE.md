# PaidYet

A Telegram bot, run on a laptop, that reminds a friend to pay people back. Local Gemma (Ollama) reads a bill photo or a Hinglish IOU line, plain code validates the result, the user taps Save, and one durable Temporal workflow per reminder nudges until they tap Paid. Sentry traces the agent. Built for the DEV "Hacktoberfest Weekend Challenge: Build for a Friend" (Gemma, Temporal and Sentry Agent Tracing categories).

## Commands

- `uv run poe up`: start Ollama + Temporal, run the app, stop both on exit or Ctrl-C
- `uv run poe run`: app only · `start-deps` / `stop-deps`: the daemons only
- `uv run poe test`: unit tests, no services needed
- `uv run poe test-integration`: against a running `poe up`

## Layout

`main.py` wires everything. In `paidyet/`: `config.py` (settings from env), `extract.py` (Gemma call + deterministic validation), `workflows.py` (ReminderWorkflow, no I/O), `activities.py` (all side effects), `bot.py` (Telegram handlers), `store.py` (SQLite). Runtime data lives in `.data/` (gitignored).

## Rules

- **Privacy.** Image bytes, base64 and raw model/OCR text never go into Temporal inputs/results, SQLite, logs or Sentry. A photo lives only in `.data/tmp/` under a random name until extraction finishes (success or final failure), then it's deleted; `main.py` sweeps `.data/tmp/` on start. Persist only confirmed fields plus Telegram chat/message IDs.
- **Sentry.** `send_default_pii=False`, `include_local_variables=False`, no prompts or responses on spans, httpx integration disabled (Telegram URLs carry the bot token). Keep the `httpx`/`httpcore` loggers at WARNING for the same reason.
- **Ollama stays on loopback.** `config.py` and `start-deps` both refuse a non-loopback `OLLAMA_HOST`.
- **The model never decides alone.** Plain code parses dates in Asia/Kolkata, rejects past due dates and checks amounts.
- **Temporal sandbox.** `workflows.py` imports no I/O libraries; side effects go in `activities.py`.
- **Secrets.** Never read or print `.env`; only edit `.env.example`.
- **Dependencies.** uv only, exact pins, `exclude-newer` cooldown in `pyproject.toml`; propose before adding anything. Tools come from Homebrew and poe tasks call Homebrew's binaries by path; Python comes from pyenv (`python-downloads = "never"`).
- **Tests.** Unit tests use Temporal's time-skipping environment; its test-server binary downloads into `.pytest_cache/temporal/` (repo-local, gitignored). Integration tests fail loudly, never skip, when the stack is down.
- Keep `docs/BUILD_LOG.md` (decisions, dead ends, real numbers) and `README.md` current.
- DevRelay rules for this session are in `.claude/devrelay-rules.md` (gitignored).
