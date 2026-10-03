---
title: "PaidYet: for the friend who says “kal bhej dunga”"
published: false
tags: devchallenge, weekendchallenge, hf26challenge
---

*This is a submission for the [Hacktoberfest Weekend Challenge: Build for a Friend](https://dev.to/challenges/hacktoberfest-weekend-2026-10-01)*

## What I Built

Meet Arjun. Arjun is a composite of friends we all have: generous, first to split the bill, the one who books the trip. When it's his turn to pay someone back, he says "haan bhai, kal bhej dunga" ("yeah bro, I'll send it tomorrow"), and he means it. Then tomorrow comes and the reminder is buried under forty messages in a group chat.

It's never about the money. Arjun has it. His share of a dinner someone else paid for, the ₹500 a friend lent him, his part of the flat's electricity and broadband: the intention is real, but the reminder dies in a chat.

**PaidYet** is a Telegram bot that runs on my laptop and doesn't let that happen:

- Arjun forwards a bill, sends a screenshot, or types "Rahul ko 500 dene hai Friday tak" ("I owe Rahul 500 by Friday").
- **Gemma**, running locally, reads it. The bot replies with what it read and the whole plan ("₹500 to Rahul · due Fri 9 Oct · I'll remind you Thu 8 Oct 7 PM, Fri 9 Oct 10 AM and 7 PM, then every morning until it's paid"), and he taps **Save**. <!-- TODO: match the wording to the real screenshot -->
- A **Temporal** workflow then reminds him: three days ahead for anything far off, the evening before, twice on the day, and every morning once it's overdue, until he taps **Paid**. **Snooze** offers Tomorrow, In 3 days, or a date picker. Nothing arrives between 10 PM and 8 AM.
- When I know he owes something, like our trip split, I add it for him as the admin. Every reminder says whether *he* added it or *I* did, so nothing feels like it came out of nowhere.

He's on Android, he's already on Telegram, and he writes in Hinglish. So: no app to install, no account to create, and Hinglish works.

## Demo

<!-- TODO: embed the video once it's recorded, e.g. {% youtube VIDEO_ID %} -->

<!-- TODO: 3 screenshots: the confirm message, a reminder with Paid/Snooze, /due -->

## Code

{% github prateek11rai/paidyet %}

## How I Built It

```
Telegram ──▶ bot ──▶ Temporal: ReminderWorkflow (one per due)
                       ├─ activity: read  ──▶ Gemma 4 (Ollama, 127.0.0.1)  ──▶ code checks the answer
                       ├─ activity: delete the photo / forget the text
                       ├─ wait for Save / Fix                 (signals)
                       └─ timers: remind, nudge daily         (Paid / Snooze signals, status query)
Sentry: one trace from the Telegram update through every activity, Gemma calls as gen_ai spans
```

Python, [python-telegram-bot](https://python-telegram-bot.org), [Temporal](https://temporal.io), [Ollama](https://ollama.com) with **gemma4:e4b**, and [Sentry](https://sentry.io). It runs on an Apple M4 with 16 GB, and `uv run poe up` starts everything. I built it with Claude Code as my pair programmer; the session is embedded below.

### Gemma reads, code decides

Gemma fills a JSON schema through Ollama's structured output. Then plain code takes over: it parses dates in Asia/Kolkata ("Friday tak", "kal", "3 din mein" and "in 2 minutes" are all parsed by code, not the model), rejects past due dates and odd amounts, and decides whether the draft can even be saved.

I made four synthetic bills, each with a trap: a bill date next to the due date, a late-fee amount, a "next invoice" date, and an insurance notice with a *grace period end*. I also wrote six Hinglish and English IOU lines. Here's how the iterations went, on 10 samples:

| Version | Due date right | Silently wrong due date | Warm photo |
|---|---|---|---|
| Baseline prompt | 8/10 | 2 | 4.8 s |
| Thinking on | 8/10 | 2 | ~33 s |
| Model lists every date with its label | 8–9/10 | 1 (12 Oct became **12 Dec**) | 8–11 s |
| **Gemma quotes the deadline, code parses it, one hinted retry** | **9/10** | **0** | **~7 s** |

The one that's still "wrong" is the insurance notice. Gemma keeps reaching for the grace-period date, but now it fails *safely*: the bot says "The date I found looks like the end of a grace period, not the due date" and shows only **Fix**, no Save. A silently wrong due date is the one failure a payment reminder can't afford. A blocked one costs a tap.

This matches what [Taranpreet Kaur](https://dev.to/taranpreet_kaur_4b538d878) describes in [Why an LLM Alone Cannot Do Invoice Extraction Yet](https://dev.to/taranpreet_kaur_4b538d878/why-an-llm-alone-cannot-do-invoice-extraction-yet-1oa5): models show "silent confidence", returning a plausible value instead of nothing, so you want "model for reading, code for checking". Gemma said `confidence: 1.0` on the grace-period date.

### Temporal makes the reminder durable

Every due is one `ReminderWorkflow`, started the moment a message arrives:

- **Reading the bill is an activity with retries.** A cold model load takes ~22 s, and a bad JSON reply happens. Temporal retries both, up to three attempts.
- **The photo is deleted in a `finally`.** That runs on success *and* after the final failed retry.
- **Save, Fix, Paid and Snooze are signals, and `status` is a query.** The bot asks the workflow who added a due before it lets anyone tap Save, and who owes it before Paid.
- **Reminders are durable timers.** For a date: 10 AM three days before, 7 PM the evening before, 10 AM and 7 PM on the day, then 10 AM daily while overdue, never between 10 PM and 8 AM. After 30 days overdue, one last message, and then it waits quietly in `/due`. A due set less than 10 minutes ahead is a demo: it repeats every few minutes, five times, so the whole cycle fits in a video without a fake clock.
- **Snooze only postpones.** The workflow ignores a snooze that would land before the next scheduled reminder, and the bot says so.
- **Long-overdue dues continue-as-new,** carrying their state over, so a month of nudges never bloats one history.

My laptop sleeps. Telegram holds updates for 24 hours, and Temporal fires missed timers when it wakes, so a reminder can be late but never lost. To prove it, I killed the worker mid-wait and restarted it:

<!-- TODO: log excerpt + screenshot from the kill-and-restart run -->

The unit tests run the workflow in Temporal's time-skipping test server, so "nudge daily for four days" takes milliseconds.

### Sentry shows the agent's work, not the bill

Each read is an `invoke_agent` span. Inside it are a `gen_ai.chat` span per Gemma call, carrying token counts and how Ollama's time split between loading the model, reading the prompt and generating, and `execute_tool` spans for the code checks. So the trace shows exactly where code overrode the model. Temporal interceptors carry one trace from the Telegram update into every activity, including a reminder that fires days later.

<!-- TODO: Sentry trace screenshot(s) with timings and tokens, no contents -->

What the traces showed:

- **The first message after idle was slow, and it wasn't Gemma's fault.** The first call took 29.5 s, and 22.2 s of it was Ollama *loading the model*; generating took ~4 s. The fix is a warm-up request at start-up. <!-- TODO: confirm with the live Sentry screenshot -->
- **The hinted retry is visible.** A read where code asked Gemma again shows two chat spans and `retry_reason: no_due_date`. It costs a few seconds on the bills that need it and nothing on IOUs. <!-- TODO: confirm the cost from live traces -->
- **Cost: ₹0.** Tokens are counted for latency, not billing.

Sentry never sees the bill. Spans carry model names, token counts, timings and outcome categories, never prompts, replies, amounts or names. A unit test sends a full read through Sentry's real configuration and checks that none of it leaks.

## Why Does Open Innovation Matter?

Because these are bills. A bill photo has a name, an address, an account number and what someone owes. With an open-weight model on my laptop, no AI company reads Arjun's electricity bill, keeps it, or bills me for reading it. Reminding someone about ₹500 shouldn't cost API credits, and it doesn't: each read is a few seconds of my laptop's time.

Open also meant I could change how the agent behaves. I turned thinking off when it cost 6–8x the latency for no accuracy gain, pinned temperature to 0, and can swap `gemma4:e4b` for `gemma4:e2b` with one variable on a smaller machine. Anyone can run their own copy: a friend can self-host it with their own BotFather token and be their own admin.

One honest caveat: Telegram bot chats are **not** end-to-end encrypted, so the photo Arjun sends also stays in his Telegram chat, on Telegram's servers. My claim isn't "it never leaves the laptop". It's that no model provider ever gets it. On my side, the photo exists only in a temp folder under a random name until Gemma has read it, and what Arjun types never reaches Temporal's history. What's stored is the confirmed title, payee, amount, due date and kind.

## What I Got Wrong

- **I trusted the model with the date.** Version one read the insurance notice's *grace period end* as the due date, at `confidence: 1.0`. Asked plainly to list the dates, Gemma reads "Renew by: 20-10-2026" perfectly. The mistake is in *choosing*, so choosing moved to code.
- **I assumed thinking would help.** It made photos take ~33 s instead of ~5 s and fixed nothing.
- **I made the model do more work, and it got worse.** Asking it to list every date with its label turned "12 Oct" into 12 December, a silent error. Dropped within the hour.
- **I quoted the bill into my own database.** An early "blocked" message read "The date I found is labelled 'Grace period ends 19-11-2026'", and that message is stored in Temporal's history. It now names a category instead.
- **Ctrl-C left Ollama and Temporal running.** poe re-sends SIGINT to the task's process group every 0.8 s until it exits, which killed my own cleanup halfway. The cleanup now ignores SIGINT and runs in its own process group.
- **My token scrubber had a `\b` in it.** In `…/bot123456:ABC…` there's no word boundary between "bot" and the digits, so it would have let a bot token in a URL through to Sentry. A test caught it.

The full log, with every number and dead end, is in [docs/BUILD_LOG.md](https://github.com/prateek11rai/paidyet/blob/main/docs/BUILD_LOG.md).

## My Agent Session

<!-- TODO: {% agent_session SESSION_ID %} once the curated session is approved and submitted -->

## Prize Categories

- **Best Use of Gemma:** Gemma 4 (e4b) running locally in Ollama reads bill photos and Hinglish IOUs through structured output, with code checking every answer and asking Gemma again when it spots a known mistake.
- **Best Use of Temporal:** One durable workflow per due: Gemma reading is a retried activity, inputs are deleted in a `finally`, Save/Fix/Paid/Snooze are signals, status is a query, and reminders survive the laptop sleeping and the worker being killed.
- **Best Use of Sentry Agent Tracing:** `invoke_agent`, `gen_ai.chat` and `execute_tool` spans with tokens and Ollama's timing breakdown, one trace from Telegram through Temporal into every activity, and no bill contents in Sentry, enforced by a test.
