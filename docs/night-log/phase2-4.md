# Night log: Phases 2 and 4 (branch `night/phase2-4`)

Built on `origin/main` at 57275cb (Phase 1 alerts). Not merged.

## What was built

**Phase 2, the watcher** (`brain/app/watcher.py`, scheduled every `WATCH_MINUTES`):

| Rule | Level | Key |
|---|---|---|
| Open/pending task due within 6 h (events: start time) | notify | `risk6:<id>` |
| ...within 1 h (closes the 6 h one) | alarm; **call at night** for accepted (`open`) tasks | `risk1:<id>` |
| Promise you made, past due (last 24 h), still open | notify | `promise:<id>` |
| Bill (₹/Rs/INR/fee/bill/payment/EMI/premium/invoice), 2 days before | notify | `bill2:<id>` |
| Bill on the due day | alarm | `billday:<id>` |
| Renewal/expiry task or mail, 7 days before / on the day | notify | `renew:`, `renewday:`, `renewmsg:`, `renewmsgday:` |
| WhatsApp silent > 12 h while ≥ 20 msgs/week normally | notify, once a day | `health:whatsapp:<date>` |
| Gmail poll failing ≥ 3 times over ≥ 30 min | notify, once a day | `health:gmail:<label>:<date>` |
| Alarm unanswered after 5 min (sent < 2 h ago) | call, ≤ 2/day | `call:<alarm id>` |
| Unanswered question from family / professor / advisor / manager / VIP | alarm instead of notify | `question:<id>` |

Decisions:
- **A key the watcher ever raised is never raised again**, answered or not; a task you dismissed
  once is left alone by every watcher rule. A task you already heard about at ≥ notify in the last
  6 h gets no 6 h notice (stops new_task + risk6 double pings).
- **Promises** are detected without touching the classifier: the task's source message is
  `from_me` (not the self chat), or a `from_me` message's stored classification JSON says
  `"action":"update"` with `"task_id":<id>,`.
- **Model judgement** (`WATCH_JUDGE`): one JSON yes/no to the classifier model, 8 s via
  `asyncio.wait_for`. "No" drops one level (alarm → notify, notify → silent; a call talked down is
  not made). A silenced alert is recorded as a level-0 `done` row so it is not re-asked every 5 min.
  Not applied to questions/new tasks (notify.py), only watcher rules, as specified.
- **Learning**: ≥ 3 `dismiss` answers for the same kind whose task came from the same chat
  (or no task, for health-style alerts) in 30 days → one level lower. Applied in `watcher.interrupt`,
  which notify.py's `new_task` and `unanswered_question` now use too. alerts.py untouched.
- **Who is waiting**: graph lookup by exact slug of the sender's name/id or exact alias only; close if
  an edge to the owner is in `CLOSE_RELS` (advisor_of, parent_of, sibling_of, spouse_of, manager_of…)
  or the entity summary contains a word from a fixed list (mother…cousin, professor, advisor, guide,
  supervisor, mentor, manager, boss, HOD, dean, principal, faculty, teacher). Plus `WATCH_VIP_CHATS`.
- **Calls at night**: alerts.py holds alarms through quiet hours, so an alarm could never escalate
  at night. An accepted task due within the hour inside quiet hours is raised as a call directly
  (same cap, judge applies). Question alarms never call at night; stale (> 2 h) alarms and passed
  deadlines never call.
- **Gmail health**: `gmail.record_poll()` keeps Note `gmail_error_<label>` = `{since, count, error,
  last}` while polls fail (exception, or "not authorised"), deleted on the first good poll.

**Phase 4, background work** (`brain/app/review.py`):
- Weekly review, Sunday 18:00: planner brief (done, slipped, next 7 days, renewals), plain list on
  failure; supersedes last week's (`weekly:<date>`).
- Monthly noise report, 1st 09:00: delivered alerts of last month per level, Done vs Not important,
  unanswered, dismiss rate, calls, per kind, and which targets are missed. Deterministic text, no
  model (`monthly:<yyyy-mm>`).
- Gmail drafts (`DRAFTS_ENABLED=false`): question tasks from non-bulk, non-self mail created in the
  last 2 days; planner writes the reply; saved with `drafts().create` in the original thread with
  In-Reply-To/References. Note `gmail_draft_<task id>` prevents a second draft. No send call exists.

## Tests

96 passed / 96 (61 existing + 28 in `tests/test_watcher.py` + 7 in
`tests/test_review.py`). `ruff check app tests` clean. Watcher tests run at a time 30 days ahead
of the real clock so rows other tests create "now" fall outside its windows.

## Not tested / not done

- No live run against the real database, the hub, Gemini or Gmail (by rule). The judge prompt has
  only been exercised with the fake LLM; its real yes/no rate is unknown, so watch the first days.
- Level 4 (call) is still delivered as whatever the hub does with level 4; the voice call itself
  (Twilio or similar) is Phase 3 and not built here.
- Renewal dates in messages need a year (12/11/2026, 3rd Dec 2026, 2026-12-01); "renews on 5 Nov"
  without a year is ignored on purpose. Numeric dates are read day-first.
- The weekly review and monthly report jobs are cron-scheduled but were not run under APScheduler.

## Settings (all in `.env.example`)

`WATCH_ENABLED=true`, `WATCH_MINUTES=5`, `WATCH_WHATSAPP_SILENT_HOURS=12`, `ESCALATE_MINUTES=5`,
`CALLS_PER_DAY=2`, `WATCH_JUDGE=true`, `WATCH_VIP_CHATS=`, `REVIEW_ENABLED=true`,
`DRAFTS_ENABLED=false`.

Owner to do: optionally fill `WATCH_VIP_CHATS`; turn on `DRAFTS_ENABLED` once happy (needs the
existing Google token to include `gmail.compose`, which `app.google_auth` already requests; tokens
issued before that scope was added need `python -m app.google_auth` again).
