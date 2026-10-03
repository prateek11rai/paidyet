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

## Sun 4 Oct, ~01:20: extraction with Gemma

**Samples.** `samples/` holds four synthetic bills (electricity, broadband, rent, insurance renewal) rendered from HTML with headless Chrome, and six IOU lines in Hinglish and English. Companies are fictional, and names and numbers are fake. Each bill includes a trap: a bill date, a late-fee amount, a "next invoice" date, a grace-period end, a deposit, a sum insured. `samples/expected.json` holds the answers, assuming today is Sun 4 Oct.

**How a read works.** Gemma fills a JSON schema through Ollama's structured output (`format`), at temperature 0 with thinking off. Plain code then decides:
- dates in Asia/Kolkata, including relative phrases like "Friday tak", "kal", "3 din mein" and "in 2 minutes", parsed by code rather than the model;
- no past due dates;
- the amount must be a positive number up to ₹10 lakh;
- whether the draft can be saved at all.

**Iterations** (gemma4:e4b, 10 samples, Apple M4 16 GB; time is warm wall-clock, excluding model load):

| Version | Change | Kind | Payee | Amount | Due | Blocked | Silent wrong due | Photo | Text |
|---|---|---|---|---|---|---|---|---|---|
| v1 | Baseline prompt | 10 | 8 | 10 | 8 | 0 | 2 | 4.8 s | 3.6 s |
| v2 | Quote the deadline (`due_evidence`), stricter payee rules | 10 | 9 | 10 | 8 | 0 | 2* | 4.3 s | 3.2 s |
| v2 + thinking | `think: true` | 9 | 10 | 10 | 8 | 0 | 2* | ~33 s | ~19 s |
| v3 | Code reviews the answer; one hinted retry; a bill with no date blocks Save | 10 | 9 | 10 | 9 | 1 | 0 | 6.7 s | 3.2 s |
| v4–v5 | Model also lists every date with its label | 10 | 9 | 10 | 8–9 | 1 | 1 (v5) | 8–11 s | 4.8 s |
| **final** | v3, plus code parses the date out of the quote | **10** | **9** | **10** | **9** | **1** | **0** | **7.0 s** | **3.2 s** |

\* In v2 (with or without thinking) the validator quietly defaulted a bill with no date to "tomorrow", which counts as silently wrong. After that, a bill without a due date blocks Save, and only an IOU without a date defaults to tomorrow (with a note).

What we learned:
- **The bill-date trap is real, and it's about choosing, not seeing.** v1 took the insurance *grace period end* (19 Nov) as the due date. But asked plainly to "list every date with its label", Gemma reads `Renew by: 20-10-2026` correctly. The mistake happens when it has to choose.
- **Thinking costs 6–8x and doesn't fix dates.** Off.
- **Making the model list labelled dates (v4–v5) backfired.** First it copied the dates themselves as labels. Then, once told not to, it turned "Pay by 12 Oct 2026" into 2026-12-12, a *silent* wrong date. Dropped.
- **What worked: the model quotes, code parses.** Gemma copies the words around the deadline (`"Pay by 12 Oct 2026"`), and code parses the date from that quote, day first. Labels like "grace", "bill date", "next invoice" or "after due date" are rejected. When code sees a known mistake (a bill with no date, or a non-deadline label), it asks Gemma once more with a pointed hint. That fixes the electricity bill. The insurance notice still fails after the hint, but it fails *safely*: Save is blocked with "The date I found is labelled 'Grace period ends', which isn't a due date", and the user taps Fix.
- **Cold start.** The first call took 29.5 s, of which 22.2 s was Ollama loading the model; Ollama unloads it after 5 idle minutes. Warm photos take ~7 s and text ~3 s, both well under the 20 s threshold, so **gemma4:e4b stays** and e2b isn't needed. The cold start is a UX problem to fix (warm-up on start) and to show in Sentry.
- At temperature 0, two consecutive runs gave identical fields.

**Privacy.** A photo is written to `.data/tmp/<32 random hex>.<ext>` with mode 0600, resolved only through an allowlist pattern, and deleted afterwards; `sweep()` runs on start. For Fix, Gemma gets the previous *fields* plus the user's correction, never the photo again, so a photo is needed only for the first read.

## Sun 4 Oct, ~01:35: workflow and buttons

**One `ReminderWorkflow` per reminder**, started as soon as a message arrives:
1. **Read.** The `read_input` activity runs with up to 3 attempts. A cold model load (~25 s) fits inside its 3-minute timeout, and flaky Ollama calls or bad JSON are retried.
2. **Discard the input.** Runs on success *and* on final failure, before anything else, so the photo is gone as early as possible.
3. **Confirm.** Waits for `save`/`fix` signals and expires after 7 days.
4. **Remind.** Durable timers. Signals: `paid` and `snooze(until)`. Query: `status`. The workflow is the source of truth; SQLite is a read model for `/due`.

**Schedule** (pure functions, unit-tested):
- A date due gets a reminder at 7 PM the day before. If that has passed, at 10 AM or 7 PM on the day.
- Then a nudge every day at 10 AM until Paid.
- A demo due ("in 2 minutes") fires at that time, then repeats at its own pace (from 1 minute up to 1 day). So the whole Save → remind → Snooze → remind → Paid cycle fits in a video with no fake clock.

**Message text never enters Temporal history.** The brief rules out images and raw model text in workflow payloads. We extend that to what the user *typed*: texts and Fix corrections sit in an in-memory inbox, and the workflow carries only a random key. Fix sends Gemma the previous *fields* plus the correction, so the original text and photo are needed only once. The trade-off: if PaidYet restarts between receiving a message and reading it, the input is gone (texts were in memory, photos are swept). The read then fails as non-retryable `InputGone`, and the user is asked to send it again. We chose privacy over durability for that brief window.

**Who may tap what.** Before signalling, the bot queries the workflow: Save and Fix belong to whoever added the reminder, Paid and Snooze to whoever owes it. Callback data is a type plus the reminder ID, at most 26 bytes (Telegram's limit is 64).

**Test findings:**
- Temporal's time-skipping test server downloads its binary on first use. With `download_dest_dir` set, it lands in `.pytest_cache/temporal/` (62 MB, 1.33.0); nothing goes to the system temp dir.
- **Hang:** the suite passed test by test but hung when run together. One test left its workflow waiting on a 7-day timer. When a later test skipped time, that timer fired a workflow task onto a queue with no worker, and time skipping waited on it forever. The fix: each test terminates its own workflow before its worker stops.
- The final-failure test caught the workflow notifying the user *before* deleting the input. Swapped: delete first.
- A formatting test caught `str.capitalize()` lowercasing "Fri 9 Oct" into "fri 9 oct".

106 unit tests in 2.3 s.

## Sun 4 Oct, ~01:50: access and lists

- **Allowlist gate** runs before every handler. An unknown user gets "This is a private bot. Your Telegram ID is N; send it to the owner." and nothing else: no workflow, no model call.
- **`/due`** shows "Overdue" and "Upcoming" from SQLite. Each line says "added by you" or "added by <admin>" and has a ✅ Paid button. Tapping it signals the workflow and redraws the list without that item. The admin also sees "You added for others".
- **Admin.** `/remind arjun ₹500 to Rahul by Fri`, or a bill photo captioned "for arjun". The admin confirms the draft in their own chat ("For Arjun · added by you"). On Save, Arjun gets "Prateek added a reminder for you" with Paid/Snooze, and when he taps Paid the admin gets "✅ Arjun paid: …".
- **A friend who never opened the bot.** Telegram won't let a bot message someone first. The "added" message fails as non-retryable, so the workflow tells the admin to ask the friend to send /start, stays alive, and tries again at each reminder.

## Sun 4 Oct, ~02:15: Sentry agent tracing (code; live check pending the DSN)

**What a read looks like in Sentry:**
```
telegram capture                       (transaction, bot)
├─ download photo · start ReminderWorkflow
└─ activity read_input                 (transaction, worker; same trace through Temporal headers)
   └─ invoke_agent paidyet-reader      gen_ai.invoke_agent: model, total tokens, model_calls, retry_reason, ready
      ├─ chat gemma4:e4b               gen_ai.chat: tokens in/out, ollama.load_ms / prompt_eval_ms / eval_ms
      ├─ execute_tool review_answer    code checks the answer (retry_reason: none | no_due_date | wrong_date_label)
      ├─ chat gemma4:e4b               only when review_answer asked for a hinted retry
      └─ execute_tool validate_draft   code decides dates, amount, ready / problem count
   activity discard_input · activity show_confirm · … · activity send_reminder (days later, same trace)
```
- **Trace across Temporal.** A client interceptor writes `sentry-trace`/`baggage` into the workflow-start headers. A workflow interceptor (pure, inside the sandbox) copies them onto every activity it schedules. A worker interceptor continues the trace and makes each activity a transaction, capturing *every* failed attempt, so flaky retries show up as issues.
- **Code's own decisions are visible.** `review_answer` and `validate_draft` are tool spans, so the trace shows exactly where code overrode or rejected Gemma.

**Privacy, enforced by a test.** A unit test runs a full read with Sentry pointed at an in-memory transport, using the *same* options as `main.py`. It asserts that the payee, amount, phrase, dates, label and image bytes appear nowhere in what Sentry would send. Spans carry the model name, token counts, timings, call counts and outcome categories only.

**What the tests caught:**
- **Token scrubber.** `before_send` scrubs anything shaped like a bot token. Its first regex started with `\b`, and in `…/bot123456:ABC…` there's no word boundary between "bot" and the digits, so a token inside a Telegram URL would have slipped through. Fixed with a digit lookbehind.
- **Bill text in Temporal history.** A blocked draft said "The date I found is labelled 'Grace period ends 19-11-2026'…". That message lives in the Draft, which is stored in Temporal history: model-quoted bill text. It now names a category instead ("looks like the end of a grace period, not the due date"). The hint to Gemma can still quote, because it goes only to the local model.
- **Span format.** sentry-sdk 2.70 sends gen_ai spans as streamed span-v2 items linked by trace and parent span IDs, not inside the transaction's `spans` list. The tests read both formats.

## Sun 4 Oct, ~03:00: code review before live testing

A second Claude instance reviewed the repo at `f61a137`. Prateek confirmed the schedule changes (a product decision) before they were built.

**A. The reminder schedule, redesigned.** The old one had problems: a renewal due in three weeks got its first reminder the night before. "In 5 hours" nudged every 5 hours around the clock, including 3 AM. And the demo pace leaked into real use. Now:

| Due | Reminders |
|---|---|
| Date | 10 AM three days before (only if saved at least 3 days ahead), 7 PM the evening before, 10 AM + 7 PM on the day, then 10 AM daily while overdue |
| Time, at least 1 h away | 30 min before, at the deadline, then 10 AM daily |
| Time, 10 min–1 h away | At the deadline, then 10 AM daily |
| Time, under 10 min (demo) | Every N minutes, 5 times, then 10 AM daily. N is measured from when the due was read *or fixed* (`Draft.read_at`), not from the original message |

- **Quiet hours, 22:00–08:00, for all but demos.** A reminder that must beat the deadline moves *earlier*, to 21:30; anything else moves to 08:00. (Prateek's call: moving a pre-deadline reminder to 08:00 could land it after the deadline.)
- **After 30 days overdue:** one last message with only a Paid button, then the due waits in `/due` with no timers.
- **The confirm message states the whole plan:** "I'll remind you Tue 6 Oct 10 AM, Thu 8 Oct 7 PM, Fri 9 Oct 10 AM and 7 PM, then every morning until it's paid."
- **After a long sleep,** missed reminders are skipped rather than sent in a burst: the next reminder is computed from `max(scheduled, now)`.

**B. Bugs fixed:**
- **A pending Fix or date question swallowed unrelated messages.** A brand-new IOU sent hours later could become the correction, or the old reminder's snooze date. Now only a *reply* to the bot's question (ForceReply) counts, any other message cancels the question and is read normally, and there's `/cancel`.
- **The admin's "added a reminder for you" notice had Snooze**, which could pull the first reminder *earlier*. It now offers only Paid. Separately, the workflow ignores any snooze before the next scheduled reminder, and the bot says "That's before your next reminder (…)".
- **SQLite activities retried forever** (no maximum attempts), so a deterministic bug would loop invisibly every 5 minutes. They now get 5 attempts, then the failure is logged and in Sentry, and a failed `save_reminder` tells the user. Telegram calls still retry through an offline laptop, but `BadRequest`, like `Forbidden`, is now final. `mark_paid` was split into `mark_paid` (SQLite) and `show_paid` (Telegram) so each gets the right policy. A failing `discard_input` no longer crashes the workflow; the start-up sweep is the backstop.
- **A long-overdue reminder looped in one history.** It now continues-as-new after 50 reminders (or when Temporal suggests it), carrying the draft, schedule anchors, message ID, count and snooze state, including a snooze that arrived but wasn't applied yet. With the 30-day cap a due sends at most ~35 reminders, so this is insurance.

**C.** Ollama `keep_alive` is now 30 minutes, set on *every* request. Setting it only on the warm-up wouldn't work, because each chat request would reset it to the 5-minute default. The cost is ~6.6 GB of RAM held (5.5 GB of weights plus a 1 GB vision projector); noted in the README.

**Test finding.** After these tests were added, two passed alone but failed in the suite, with `env.sleep()` timing out in real time. The cause: a test ended while an activity was in flight, and terminating a workflow mid-activity left a time-skipping lock held in the test server. The fix: `running()` waits for activities to settle before terminating.

137 unit tests (~6 s), stable across repeated runs. The integration flow now snoozes past the next demo reminder, because an earlier snooze is refused by design.

## Sun 4 Oct, ~02:30–03:05: live testing (real Telegram, Sentry, Temporal, Gemma)

**Integration tests: 5/5 pass** against the running stack in 2 min 18 s. That's real Gemma on every sample, a full text cycle, photo deletion and Fix, with Telegram messages going only to the admin, prefixed `[test]`. Getting there turned up three bugs:
1. **The first start crashed.** `deleteWebhook` during the polling bootstrap hit python-telegram-bot's 5 s read timeout, and the default of *zero* bootstrap retries aborted the app. Now it retries until it succeeds, with 10 s connect and 15 s read/write timeouts.
2. **A status race.** The workflow incremented `reminders_sent` before sending and moved `next_at` only after, so for a moment the status said "sent" with a stale `next_at`, and a snooze checked against it was refused. The next reminder is now computed first.
3. **The warm-up logged "loaded in 0 ms"**, because Ollama reports no `load_duration` for an empty prompt. It now logs wall-clock time (7.0 s from a warm disk cache).

**Live flow in Telegram, admin account:**
- **Bill photo:** a read in 7.4 s (1,098 in / 99 out tokens), then Save. Planned first reminder: Tue 6 Oct 10 AM, 3 days ahead of the Fri 9 Oct due date.
- **Bill photo, Fix "due in 2 minutes":** Fix in 3.4 s, then Save, reminder at the exact second (02:45:19), Paid 6 s later. The workflow completed `"paid"`, SQLite got `paid_at`, and the message became "✅ Paid".
- **Hinglish IOU "Rahul ko 500 dene hai in 3 minutes":** read as Rahul · ₹500 · due in 3 minutes, in 12.5 s.
- **`.data/tmp`** was empty after every read.

**Kill-and-restart proof** (IST, from Temporal's history):

| Time | Event |
|---|---|
| 02:55:39 | Saved; durable timer 101.98 s, due 02:57:21 |
| 02:56:05 | Ctrl-C: app and worker down |
| 02:57:21.0 | `TIMER_FIRED` in Temporal, with no worker running |
| 02:57:25 | Temporal and Ollama stopped too: the pending work exists only in `.data/temporal.db` |
| 02:57:40 | `uv run poe up` again; worker up at 02:57:44.8 |
| 02:57:42.6 | `WORKFLOW_TASK_TIMED_OUT`: the task had been routed to the dead worker, so Temporal re-dispatched it |
| 02:57:45.6 | Reminder sent, **24.6 s late, not lost**, under 1 s after the worker came back |

**The Ctrl-C dead end, properly understood this time.** During the proof, Ctrl-C left Ollama and Temporal running again. The earlier explanation (poe re-sends SIGINT every 0.8 s) was only half of it. poe's shutdown loop also *raises the urgency by itself* every 0.8 s, and at level 3 sends **SIGKILL** to the task's process group. Anything that takes more than ~1.6 s to shut down is killed, cleanup included. The Temporal worker with live workflows takes about that long; the earlier test passed only because the app exited instantly. Reproduced 3/3 in a scratch project with a fake app that needs 2 s to stop.

The fix: `up` is now a `cmd` task with `use_exec = true`, so poe hands the process over to `bash -c "$UP_SCRIPT"` and isn't around to escalate. Verified 3/3 in the scratch project, then on the real stack: Ctrl-C with live workflows stopped everything in 3 s, with no pid files left.

## Sun 4 Oct, ~03:10: a portable uv.lock

`uv.lock` pointed every package at a private package mirror this laptop uses, which nobody else can install from. The fix:
- **Rewrite** the mirror's registry and file URLs to `https://pypi.org/simple` and `https://files.pythonhosted.org/packages/`, which use the same path layout.
- **Verify** against PyPI's JSON API (read-only): all 71 pinned files across 25 packages exist at the rewritten URLs with identical sha256.
- **Fix every commit**, since the repo goes public: a backup bundle, then `git filter-repo --replace-text` over all 12 commits. Afterwards the mirror appears 0 times in `git log -p --all`; author, dates and messages are unchanged; and `uv.lock` is the only path that differs in any commit. Force-pushed with a lease pinned to the old head while the repo was still private.

**Why it kept coming back.** Two things re-lock silently on a machine whose uv uses a mirror as its default index:
1. Plain `uv run`: a PyPI-pointing lock looks stale to it, and an offline test confirmed a re-lock writes all 96 URLs back to the mirror.
2. **poe itself:** it auto-detects uv projects and runs every task through `uv run`, without `--frozen`, so even `uv run --frozen poe test` re-locked.

Now:
- poe's executor is set to `uv` with `frozen = true`;
- CLAUDE.md says to use `uv run --frozen …` on such a machine;
- a unit test fails if `uv.lock` names any host other than PyPI's two.

138 unit tests.
