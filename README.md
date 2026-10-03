<p align="center"><img src="docs/img/logo.svg" alt="PaidYet logo" width="120"></p>

# PaidYet

**Keeps reminding your friend until they've paid you back.**

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](.python-version)

PaidYet is a Telegram bot that runs on your laptop. Send it a bill photo or a line like "Rahul ko 500 dene hai Friday tak". [Gemma](https://ai.google.dev/gemma), running locally in [Ollama](https://ollama.com), reads it. You tap Save, and a durable [Temporal](https://temporal.io) workflow keeps reminding you until you tap Paid. [Sentry](https://sentry.io) traces every step, with no bill contents.

## Requirements

- macOS or Linux, [Homebrew](https://brew.sh), [uv](https://docs.astral.sh/uv/), Python 3.12, and a Telegram account.

## Setup

```sh
brew install ollama temporal uv
git clone https://github.com/prateek11rai/paidyet && cd paidyet
uv sync
cp .env.example .env   # then fill in the values; each one is explained in the file
```

## Run

```sh
uv run poe up
```

This starts Ollama (on 127.0.0.1 only) and the Temporal dev server (UI at http://127.0.0.1:8233), then runs the bot in the foreground. Ctrl-C stops the bot and both servers. It runs without a bot token or Sentry DSN too, and logs that it skipped them.

| Task | What it does |
|---|---|
| `uv run poe up` | Start everything; Ctrl-C stops everything |
| `uv run poe run` | Run the app only |
| `uv run poe start-deps` / `stop-deps` | Start or stop Ollama and Temporal only |
| `uv run poe test` | Unit tests |
| `uv run poe test-integration` | Integration tests against a running `poe up` |

## License

MIT for the code. The logo is Lucide's `badge-indian-rupee` (ISC), see [docs/img/logo-LICENSE.txt](docs/img/logo-LICENSE.txt).
