# The learning harness

How the assistant learns, adapts and grows from what happens when you use it, without ever
loosening its safety rules. Code: `brain/app/learn.py`, API: `brain/app/routers/learn.py`, app:
Settings -> Learning and the 👍 / 👎 under every answer.

```
 request ──> thinking loop ──> answer
    │            ▲  lessons that fit this request (active + trial)
    ▼            │
 Episode ──signals──> 👍 👎 · "no, I meant…" · "from now on…" (taught at once)
    │
    ▼  nightly (LEARN_HOUR, 03:40)
 settle   lessons thumbed down ≥ 2 times (and more than up) turn off
 review   a model scores answers nobody judged: 8+ become examples, 5 or less go on to reflect
 reflect  a model reads the episodes with a signal or a weak review -> lessons + regression cases
 practice past requests replayed in the sandbox with what was learnt; a model coaches the tries -> scores + lessons
 gate     agent_eval with the lessons on trial: worse than the last good run -> roll back, else promote
```

## Pieces

| | What | Where it lives |
|---|---|---|
| Episode | One request: what was asked, the tools used, the answer, the lessons in its prompt, the outcome and your note | `episode` table |
| Lesson | `preference`, `fact`, `skill` (how to do a kind of job), `rule`, `voice` (how to speak); `when` words say when it applies | `lesson` table |
| Learnt case | A regression case from a real answer that went wrong | `learnedcase` table, run by `agent_eval --learned` |
| Harness run | One reflection or evaluation and what it changed | `harnessrun` table |

## Lesson lifecycle

- **taught** ("from now on…", "next time…", a sentence that starts with always / never, "remember that I…"): active at once.
- **written by you** in the app: active at once.
- **reflected** preferences, facts and voice lessons with confidence ≥ `LEARN_TRIAL_CONFIDENCE` (0.7): **trial**,
  in the prompt marked "(on trial)", until the next evaluation promotes them or rolls them back.
- **reflected** skills, rules or anything less certain: **proposed**, until you tap Keep in the app.
- **off**: you turned it off, it kept getting 👎, or the evaluation rolled it back. The note says which.

The same lesson said or found again reinforces the existing one (word overlap ≥ 0.6) instead of adding a copy.

## Growing without feedback

The model is never retrained. What grows is what it is given and how that was tested:

- **Signals by themselves:** the same thing asked again within 10 minutes, the step limit, a tool that failed. Hints only: they count against no lesson.
- **Examples:** answers you liked, or that scored 8+, are shown (tool names and the start of the answer) when a similar request comes. A 👎 or a retry takes one back.
- **Practice:** up to `LEARN_PRACTICE_DRILLS` past requests a night, weak spots first. A passed one waits 2, 4, 8… days; one that went badly comes back tomorrow. Requests you marked good are left alone, and any touching passwords, payments and the like are never replayed. It runs in `agent_eval`'s sandbox: fake devices, canned answers from mail, shop, phone and builds, a throwaway database that is deleted afterwards. Nothing real happens. The memory that request would call up is copied in, so it isn't blind.
- **Lessons by record:** wins and losses move a lesson's confidence and its place among the 8 in the prompt.

Where a lesson can come from decides how far it gets by itself:

| Found by | On trial by itself | Waits for you |
|---|---|---|
| Your signal | preference, fact, voice (confidence ≥ 0.7) | skill, rule |
| Its own review | preference, voice (confidence capped at 0.75) | fact, skill, rule |
| Practice | preference, voice, skill | rule; facts are dropped, since the sandbox's are made up |

A lesson resting only on spoken requests waits for you too, since they may be misheard.

**Your draft edits:** what you really sent against what the hub drafted, for mail and WhatsApp replies, comes here as an episode (`POST /learn/drafts`). Their message to you is never included, so nobody can steer a lesson by what they write. A `writing` lesson says how you write differently, waits in the app, and goes into use when a second edit shows the same habit, or when you tap Keep. The hub reads them from `GET /learn/writing`. The evaluation can't test these, so it leaves them alone.

**The gate, one by one:** when a night's evaluation gets worse with several lessons on trial, up to 3 are run alone, without the other new ones. Each is kept or rolled back on its own result, and the rest stay on trial for the next night.

Scores are one model's opinion of another's work, so the trend in Settings -> Learning is a guide, not proof. Your signal always outranks them.

## Safety

Lessons can never be about approvals, asking first, skipping checks, passwords, tokens, keys, deleting,
payments or ignoring rules (`learn.UNSAFE`). Such lessons are dropped at every door (taught, reflected,
written in the app). In the prompt, lessons come after the rules and the prompt says the rules win.

## Running it

- Nightly by the scheduler (`LEARN_ENABLED`, `LEARN_HOUR`); or now: `POST /learn/run` (Settings -> Learning -> Learn now).
- The evaluation costs model calls (17 built-in cases + the learnt ones). `LEARN_EVAL_ENABLED=false` skips it
  (trial lessons then stay on trial); `LEARN_EVAL_BUILTIN=false` runs only the learnt cases; `LEARN_EVAL_MODEL=codex`
  runs it on your ChatGPT session through the hub.
- By hand: `python -m app.agent_eval --learned learned.json` where the JSON is `GET /learn/export`'s shape.
