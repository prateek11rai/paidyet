---
title: "PaidYet: for the friend who says “kal bhej dunga”"
published: false
tags: devchallenge, weekendchallenge, hf26challenge, opensource
---

*This is a submission for the [Hacktoberfest Weekend Challenge: Build for a Friend](https://dev.to/challenges/hacktoberfest-weekend-2026-10-01)*

## What I Built

Arjun is a composite of friends we all have. He's generous, he's first to say "I'll pay my share", and when you remind him he says "haan bhai, kal bhej dunga" ("yeah, I'll send it tomorrow"). He means it. Then the reminder sinks under forty messages in a group chat, and nobody wants to be the one who asks twice.

It's never about the money. His share of a dinner someone else paid for, the ₹500 a friend lent him, his part of the flat's electricity and broadband: the intention survives, the reminder doesn't.

**PaidYet** is a Telegram bot that runs on my laptop and does the remembering for him:

- Arjun sends a bill photo, forwards a message, or types "Rahul ko 500 dene hai Friday tak" ("I owe Rahul 500 by Friday").
- **Gemma**, running locally, reads it. The bot replies with what it understood and the whole reminder plan, and he taps **Save**.
- A **Temporal** workflow reminds him on that plan until he taps **Paid**. **Snooze** postpones; it never pulls a reminder earlier.
- When I know he owes something, like our trip split, I add it for him as the admin, and every message says who added it.

He's on Android, he already lives in Telegram, and he writes in Hinglish. So there's nothing to install and nothing new to learn.

## Demo

<!-- TODO: replace with the video once it's uploaded, e.g. {% embed https://youtu.be/VIDEO_ID %} -->

![The bot reads a synthetic electricity bill back with the reminder plan, and Save / Fix buttons](https://raw.githubusercontent.com/prateek11rai/paidyet/main/docs/img/telegram-confirm.png)
<!-- TODO docs/img/telegram-confirm.png: confirm message for samples/electricity.png. Crop the chat header (your name, username, avatar). -->

![A reminder with Paid and Snooze](https://raw.githubusercontent.com/prateek11rai/paidyet/main/docs/img/telegram-reminder.png)
<!-- TODO docs/img/telegram-reminder.png: a ⏰ reminder with ✅ Paid / 😴 Snooze, ideally after Snooze ("😴 Snoozed until…"). Crop the chat header. -->

![The same reminder after tapping Paid](https://raw.githubusercontent.com/prateek11rai/paidyet/main/docs/img/telegram-paid.png)
<!-- TODO docs/img/telegram-paid.png: "✅ Paid on Sun 4 Oct: …". Crop the chat header. -->

![/due lists what's overdue and upcoming, and who added each one](https://raw.githubusercontent.com/prateek11rai/paidyet/main/docs/img/telegram-due.png)
<!-- TODO docs/img/telegram-due.png: the /due reply. Crop the chat header. -->

All the bills in these screenshots are synthetic: fictional companies, fake numbers.

## Code

{% github prateek11rai/paidyet %}

## How I Built It

```
Telegram ──▶ bot ──▶ Temporal: ReminderWorkflow, one per due
                       ├─ activity: read ──▶ Gemma 4 in Ollama (127.0.0.1) ──▶ code checks the answer
                       ├─ activity: delete the photo / forget the text
                       ├─ wait for Save or Fix                (signals)
                       └─ durable timers: remind on the plan  (Paid / Snooze signals, status query)
Sentry: one trace from the Telegram update through every activity; Gemma calls as gen_ai spans
```

It's Python, built on [python-telegram-bot](https://python-telegram-bot.org), [Temporal](https://temporal.io), [Ollama](https://ollama.com) running **gemma4:e4b**, and [Sentry](https://sentry.io). It runs on an Apple M4 with 16 GB. `uv run poe up` starts Ollama (bound to 127.0.0.1), a Temporal dev server and the bot, and Ctrl-C stops all three.

![Terminal: uv run poe up starting Ollama, Temporal and the bot](https://raw.githubusercontent.com/prateek11rai/paidyet/main/docs/img/terminal-up.png)
<!-- TODO docs/img/terminal-up.png: the poe up start-up lines (ollama / temporal / Sentry enabled / polling as @… / PaidYet is up). Nothing to crop: no secrets are logged. -->

### Gemma reads, code decides

Gemma fills a JSON schema through Ollama's structured output, at temperature 0 with thinking off. Then plain code takes over. It parses the dates in Asia/Kolkata ("Friday tak", "kal", "3 din mein" and "in 2 minutes" are all parsed by code, not the model), rejects past due dates and odd amounts, and decides whether the draft can be saved at all.

I made four synthetic bills, each with a trap: a bill date next to the due date, a late-fee amount, a "next invoice" date, and an insurance notice with a grace-period end. I also wrote six Hinglish and English IOU lines. Here's how my iterations scored on those 10 samples:

| Version | Due date right | Silently wrong due date | Warm photo |
|---|---|---|---|
| Baseline prompt | 8/10 | 2 | 4.8 s |
| Thinking on | 8/10 | 2 | ~33 s |
| Model lists every date with its label | 8–9/10 | 1 ("12 Oct" became 12 Dec) | 8–11 s |
| **Gemma quotes the deadline, code parses it, one hinted retry** | **9/10** | **0** | **~7 s** |

The final version scores 10/10 on amount and kind and 9/10 on payee. The one due date it still misses is the insurance notice: Gemma keeps reaching for the grace-period date. But now it fails *safely*. The bot says "The date I found looks like the end of a grace period, not the due date" and offers only **Fix**, no Save. A silently wrong due date is the one failure a payment reminder can't afford; a blocked one costs a tap.

On the live bot, a bill photo took 7.4 s (1,098 tokens in, 99 out), a typed correction 3.4 s, and a Hinglish line 3 to 12 s. Gemma 4 e4b holds about 6.6 GB of RAM while loaded (5.5 GB of weights plus a 1 GB vision projector), so PaidYet keeps it loaded for 30 minutes after each read instead of paying a ~20 s cold load every time.

This matches what [Taranpreet Kaur](https://dev.to/taranpreet_kaur_4b538d878) describes in [Why an LLM Alone Cannot Do Invoice Extraction Yet](https://dev.to/taranpreet_kaur_4b538d878/why-an-llm-alone-cannot-do-invoice-extraction-yet-1oa5): models show "silent confidence", returning a plausible value instead of nothing, so you want "model for reading, code for checking". Gemma reported `confidence: 1.0` on the grace-period date.

### Temporal makes the reminder durable

Every due is one `ReminderWorkflow`, started the moment a message arrives:

- **Reading the bill is an activity with retries.** A slow model load or a bad JSON reply just gets another try, up to three.
- **The input is deleted in a `finally`.** The photo goes on success *and* after the final failed retry. What Arjun typed never enters Temporal's history: the workflow carries a random key, and the text waits in memory until it's read.
- **Save, Fix, Paid and Snooze are signals; `status` is a query.** Before signalling, the bot asks the workflow who added a due (before Save) and who owes it (before Paid).
- **Reminders are durable timers on a plan the confirm message spells out:**

| Due | Reminders |
|---|---|
| A date ("Friday tak", a bill due 12 Oct) | 10 AM three days before (if it's at least 3 days away), 7 PM the evening before, 10 AM and 7 PM on the day, then 10 AM daily while overdue |
| A time at least an hour away | 30 minutes before and at the deadline, then 10 AM daily |
| Under 10 minutes (demo mode) | Every N minutes, 5 times, then 10 AM daily |

Nothing goes out between 10 PM and 8 AM, except demos. A reminder that has to beat the deadline moves earlier, to 9:30 PM. After 30 days overdue, PaidYet sends one last message, and the due then waits quietly in `/due`. A long-overdue due continues-as-new, so its history stays short.

My laptop sleeps and restarts, so I tested exactly that. I saved "Rahul ko 500 dene hai in 3 minutes", then pressed Ctrl-C on the whole stack, Temporal included, before the reminder was due:

| Time (IST) | What happened |
|---|---|
| 02:55:39 | Saved; durable timer set for 02:57:21 |
| 02:56:05 | Ctrl-C: bot and worker down |
| 02:57:21 | Temporal records the timer firing, with no worker to act on it |
| 02:57:25 | Temporal stopped too; the pending work exists only in `.data/temporal.db` |
| 02:57:40 | `uv run poe up` again |
| 02:57:45.6 | The reminder arrives: **24.6 s late, not lost** |

![Temporal UI: the workflow's history with the save signal, the timer and the late reminder](https://raw.githubusercontent.com/prateek11rai/paidyet/main/docs/img/temporal-history.png)
<!-- TODO docs/img/temporal-history.png: History tab, Timeline or Compact view, events collapsed. Do NOT show the Input panel, Queries → status, or expanded show_confirm / send_reminder events: they contain your Telegram user and chat IDs. -->

There are 138 unit tests (about 6 s). They run the workflow in Temporal's time-skipping test server, so "nudge daily for a month" takes milliseconds. Five integration tests run the real stack (Gemma, Temporal, Telegram) in about 2 minutes.

### Sentry shows the agent's work, not the bill

Each read is an `invoke_agent` span. Inside it are a `gen_ai.chat` span per Gemma call, carrying token counts and how Ollama's time split between loading the model, reading the prompt and generating, and `execute_tool` spans for the two code checks (`review_answer`, `validate_draft`). So the trace shows exactly where code overruled the model. Temporal interceptors carry one trace from the Telegram update into every activity, including a reminder that fires days later.

![Sentry trace: telegram capture → activity read_input → invoke_agent → chat gemma4:e4b and the tool spans](https://raw.githubusercontent.com/prateek11rai/paidyet/main/docs/img/sentry-trace.png)
<!-- TODO docs/img/sentry-trace.png: Explore → Traces, the ~02:55 IST trace, with the chat span's attributes open (model, tokens, ollama.*_ms). Crop your org and project names from the header if you prefer. -->

What the timings showed me:

- **The first message after idle was slow, and it wasn't Gemma's fault.** One cold call took 29.5 s, and 22.2 s of that was Ollama loading the model; the read itself took about 7 s. The fix was a warm-up at start-up and a 30-minute `keep_alive`.
- **The hinted retry is visible.** A read where code asked Gemma again shows two chat spans and `retry_reason: no_due_date`. It costs a few seconds on the bills that need it and nothing on IOUs.
- **Cost: ₹0.** Tokens are counted for latency, not billing.

Sentry never sees the bill. Spans carry model names, token counts, timings and outcome categories; never prompts, replies, amounts or names. A unit test runs a full read through Sentry configured exactly like the app, against an in-memory transport, and checks that none of that leaks. A scrubber also removes anything shaped like a bot token.

## Why Does Open Innovation Matter?

Because these are bills. A bill photo has a name, an address, an account number and what someone owes. With an open-weight model on my laptop, no AI company reads Arjun's electricity bill, keeps it, or bills me for reading it. A reminder about ₹500 shouldn't cost API credits, and here each read is a few seconds of my laptop's time.

Open also meant I could change how the agent behaves. I turned thinking off when it cost 6–8x the latency for no accuracy gain, pinned temperature to 0, and a smaller machine can switch to `gemma4:e2b` with one variable. Anyone can run their own copy: a friend can self-host it with their own BotFather token and be their own admin.

One caveat, stated plainly: Telegram bot chats are **not** end-to-end encrypted, so the photo Arjun sends also stays in his Telegram chat, on Telegram's servers. My claim isn't "it never leaves the laptop". It's that no model provider ever gets it. On my side, the photo exists only in a temp folder under a random name until Gemma has read it, and what's stored afterwards is the confirmed title, payee, amount, due date and kind.

## What I Got Wrong

- **I trusted the model with the date.** Version one read the insurance notice's *grace period end* as the due date, at `confidence: 1.0`. Asked plainly to list the dates, Gemma reads "Renew by: 20-10-2026" perfectly. The mistake was in *choosing*, so choosing moved to code.
- **I assumed thinking would help.** It made a photo take ~33 s instead of ~5 s and fixed nothing.
- **I made the model do more work, and it got worse.** Asking it to list every date with its label turned "12 Oct" into 12 December, a silent error. I dropped it within the hour.
- **My first reminder schedule would have nagged at 3 AM.** An insurance renewal three weeks out got its first reminder the night before, and "in 5 hours" repeated every 5 hours around the clock. A code review caught it, and the schedule above replaced it.
- **I quoted the bill into my own database.** An early "blocked" message read "The date I found is labelled 'Grace period ends 19-11-2026'", and that message is stored in Temporal's history. It now names a category instead.
- **One slow Telegram response killed the bot at start-up.** python-telegram-bot defaults to zero retries for its polling bootstrap. It now retries until it gets through.
- **Ctrl-C left Ollama and Temporal running.** My task runner escalates a Ctrl-C to SIGKILL after 1.6 s, and a Temporal worker with live workflows takes about that long to stop, so my cleanup died with it. The fix was to have the runner hand the process over instead of supervising it.

The full log, with every number and dead end, is in [docs/BUILD_LOG.md](https://github.com/prateek11rai/paidyet/blob/main/docs/BUILD_LOG.md).

## My Agent Session

I built PaidYet with Claude Code as my pair programmer, with DevRelay pulling the challenge rules live. The session below is curated. It covers the kickoff, the Gemma accuracy work, the Temporal workflow, a code review that changed the reminder schedule, live testing, and the kill-and-restart proof.

<!-- TODO: {% agent_session SESSION_ID %} once the curated session is approved and submitted -->

## Prize Categories

- **Best Use of Gemma:** Gemma 4 (e4b) runs locally in Ollama and reads bill photos and Hinglish IOUs through structured output. Code checks every answer and asks Gemma again, once, when it spots a known mistake.
- **Best Use of Temporal:** One durable workflow per due. The Gemma read is a retried activity and the input is deleted in a `finally`. Save, Fix, Paid and Snooze are signals, status is a query, and reminders survive a full stop and restart (24.6 s late, not lost).
- **Best Use of Sentry Agent Tracing:** `invoke_agent`, `gen_ai.chat` and `execute_tool` spans carry tokens and Ollama's load, prompt and generation timings, in one trace from Telegram through Temporal into every activity, with no bill contents in Sentry, enforced by a test.
