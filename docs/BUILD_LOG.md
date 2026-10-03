# Build log

Decisions, dead ends, surprises and real numbers, newest last. Times are IST.

## Sun 4 Oct, 00:36: kickoff

Challenge: DEV "Hacktoberfest Weekend Challenge: Build for a Friend", deadline Mon 5 Oct 06:59 UTC (12:29 IST). Categories: Gemma, Temporal, Sentry Agent Tracing.

What the category text asks for, and what it means for the design:

- **Temporal:** "Make your agent durable. Wrap it in a Temporal workflow so it survives failures, retries flaky tool calls, and picks up where it left off." The reminder timer alone isn't enough, so the Gemma extraction runs as a retried activity inside the same workflow, which starts when the message arrives.
- **Sentry Agent Tracing:** "how it's set up, how it performs (latency, tokens, cost), what you found, and how you debugged it." We need real traces and a real debugging story, not just a screenshot.
- **Gemma:** "run it locally". That's the whole point here.

## Toolchain

| Tool | Version | Notes |
|---|---|---|
| Ollama | 0.35.1 (Homebrew) | Newer than the 7-day release rule allows. Approved exception. |
| Temporal CLI | 1.9.1 (Homebrew, released 14 Sep) | Upgraded from 1.4.1. A stray non-Homebrew `temporal` 1.3.0 was earlier on PATH, so poe tasks call Homebrew's binary by full path. |
| Python | 3.12.2 (pyenv global) | uv is set to `python-downloads = "never"`, so it never fetches its own. |
| uv | 0.12.3 (Homebrew) | |
| Model | gemma4:e4b | Already pulled; lives in Ollama's own store. |
| Machine | Apple M4, 16 GB | |

## Dependencies

Newest release that had been public for at least 7 days (on or before 27 Sep). `[tool.uv] exclude-newer = "2026-09-27T00:00:00Z"` enforces the same cooldown for transitive dependencies. PyPI listed no advisories for any of them.

| Package | Pin | Released | Skipped as too new |
|---|---|---|---|
| python-telegram-bot | 22.8 | 12 Jun | |
| temporalio | 1.33.0 | 15 Sep | 1.34.0 (30 Sep) |
| sentry-sdk | 2.70.0 | 22 Sep | 2.71.0 (28 Sep) |
| python-dotenv | 1.2.3 | 16 Aug | 1.2.4 (1 Oct) |
| httpx | 0.28.1 | Dec 2024 | Already required by python-telegram-bot; declared because we import it |
| poethepoet (dev) | 0.48.0 | 5 Jul | |
| pytest (dev) | 9.1.1 | 19 Jun | |
| pytest-asyncio (dev) | 1.4.0 | 26 May | |

Unit tests use Temporal's time-skipping environment, Temporal's recommended way to test timers. The SDK downloads its test-server binary on first use; we point `download_dest_dir` at `.pytest_cache/temporal/` so it stays inside the repo (gitignored) and nothing is installed globally.

## Security notes

- Telegram's Bot API puts the bot token in the request URL. httpx logs every URL at INFO, and Sentry's httpx integration records URLs on spans. So `main.py` raises the `httpx`/`httpcore` loggers to WARNING and disables Sentry's httpx integration.
- Sentry runs with `send_default_pii=False` and `include_local_variables=False`. Exception frames could otherwise carry model output.
- `OLLAMA_HOST` must be loopback. Both `config.py` and `start-deps` refuse anything else.

## Scaffold: `poe up` and Ctrl-C

**Dead end.** The first `up` task stopped the deps in a bash `trap ... EXIT`. On Ctrl-C the app shut down, but Ollama and Temporal kept running. poe's shutdown loop re-sends SIGINT to the task's process group every 0.8 s until it exits, which killed `poe stop-deps` midway. The fix: the cleanup ignores INT/TERM and runs `stop-deps` in its own process group (`set -m`). The daemons are also started under `set -m`, so a Ctrl-C reaches only the app.

Verified:
- Ctrl-C stops the app, then Ollama and Temporal, and leaves no pid files.
- An Ollama that's already running is reused, gets no pid file, and survives `stop-deps`.
- `poe up` runs with no `.env`, logging one line each for the skipped bot and Sentry.
