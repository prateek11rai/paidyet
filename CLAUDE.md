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

- **Privacy.** Image bytes, base64 and raw model/OCR text never go into Temporal inputs/results, SQLite, logs or Sentry. A photo lives only in `.data/tmp/` under a random name until extraction finishes (success or final failure), then it's deleted; `main.py` sweeps `.data/tmp/` on start. Typed text (messages, Fix corrections) stays in an in-memory inbox; workflows carry only random keys. Persist only confirmed fields plus Telegram chat/message IDs.
- **Sentry.** `send_default_pii=False`, `include_local_variables=False`, no prompts or responses on spans, httpx integration disabled (Telegram URLs carry the bot token). Keep the `httpx`/`httpcore` loggers at WARNING for the same reason.
- **Ollama stays on loopback.** `config.py` and `start-deps` both refuse a non-loopback `OLLAMA_HOST`.
- **The model never decides alone.** Plain code parses dates in Asia/Kolkata, rejects past due dates and checks amounts.
- **Temporal sandbox.** `workflows.py` imports no I/O libraries and calls activities by name; side effects go in `activities.py`.
- **Who may tap what.** Save/Fix: whoever added the reminder. Paid/Snooze: whoever owes it. The bot checks via the workflow's `status` query before signalling.
- **Secrets.** Never read or print `.env`; only edit `.env.example`.
- **uv.lock stays on public PyPI.** It must only mention `pypi.org` and `files.pythonhosted.org` (`test_lockfile_points_only_at_public_pypi` enforces it). On a machine whose uv uses a private package mirror as its default index, any re-lock rewrites every URL to that mirror, so:
  - run commands as `uv run --frozen …` (e.g. `uv run --frozen poe up`); poe's own executor is already frozen in `pyproject.toml`;
  - if `uv.lock` shows up modified anyway, `git checkout uv.lock`. Never commit a re-locked file;
  - to change dependencies: lock as usual, rewrite the mirror's `…/simple/` and `…/packages/` URL prefixes to `https://pypi.org/simple` and `https://files.pythonhosted.org/packages/`, check every file's sha256 against PyPI's JSON API, and only then commit.
- **Dependencies.** uv only, exact pins, `exclude-newer` cooldown in `pyproject.toml`; propose before adding anything. Tools come from Homebrew and poe tasks call Homebrew's binaries by path; Python comes from pyenv (`python-downloads = "never"`).
- **Tests.** Unit tests use Temporal's time-skipping environment; its test-server binary downloads into `.pytest_cache/temporal/` (repo-local, gitignored). Each test must terminate its workflow before its worker stops, or a stray timer hangs time skipping for later tests. Integration tests fail loudly, never skip, when the stack is down.
- **`poe up` is a `cmd` task with `use_exec`.** Under poe's supervision, a Ctrl-C escalates to SIGKILL after ~1.6 s, killing the app mid-shutdown and the stop-deps cleanup. Keep it exec'd.
- Keep `docs/BUILD_LOG.md` (decisions, dead ends, real numbers) and `README.md` current.
- **DevRelay is wired into this repo only.** It uses a local-scope MCP server, with skills in `.claude/skills/devrelay-*` and rules in `.claude/devrelay-rules.md`, all gitignored. If they're present, follow those rules. Set it up with `uv run poe devrelay-setup` (see `docs/DEVRELAY_SETUP.md`). Never run DevRelay's skill sync without `DEVRELAY_SKILLS_DIR`, because it then writes the skills into the global skill folders. Never run `devrelay --doctor`: it re-enables the background updater, and the updater syncs the skills globally.
