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
 reflect  a model reads the episodes with a signal -> lessons + regression cases
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
