# Provenance Guard — Planning & Spec

The idea: someone hands the system a piece of text, and it figures out whether a human or
an AI wrote it. It runs the text past two different detectors, blends their scores into one
confidence number, sticks a plain-English label on it, logs the whole thing, and gives the
writer a way to appeal if the call was wrong.

This doc is where I worked out how all of that hangs together before writing any code.

---

## The path a piece of text takes

Here's what happens to one submission, start to finish:

1. A client POSTs `{text, creator_id}` to `/submit`. The app checks the body isn't empty
   and rate-limits the caller (10/min, 100/day per IP via Flask-Limiter).
2. **Signal 1** ships the text to Groq (`llama-3.3-70b-versatile`), which reads it and
   returns a probability it's AI-written plus a one-line reason.
3. **Signal 2** runs three local stats on the text — sentence-length burstiness, density
   of AI-favorite phrases, and casual-writing tells — and blends them into one score.
4. The **combiner** turns the two scores into a single confidence: `0.65·llm + 0.35·style`.
   If the LLM call fails, it falls back to the stats score alone.
5. That number picks one of three **attribution bands**, and each band has its own label
   text.
6. Everything gets **written to SQLite** — a content row (status `classified`) and an
   audit entry.
7. The **response** comes back with the `content_id`, the call, the confidence, the label,
   and the per-signal breakdown. The writer holds onto the `content_id`.

The appeal path is shorter. The writer POSTs `{content_id, creator_reasoning}` to
`/appeal`. The store flips that item's status to `under_review` and appends an appeal entry
holding the original scores plus the writer's note. The response confirms it. A reviewer
later pulls the queue from `GET /appeals`.

---

## 1. The detection signals

| | Signal 1 — LLM | Signal 2 — Stats |
|---|---|---|
| **What it looks at** | The whole passage: tone, how predictable it is, formulaic phrasing, hedging | Surface numbers you can measure directly |
| **Why it works** | A big model has read mountains of both kinds of text and knows the "sound" of generated prose | AI prose is statistically flatter — even sentence lengths, heavy formal connectives, almost no casual language |
| **What it returns** | `ai_probability` between 0 and 1, plus a reason | `style_score` between 0 and 1 |
| **Where it's blind** | Confidently wrong on AI that's been "humanized"; trips over formal-but-human academic writing; needs a network call and can be down | It's just counting — a terse polished human email looks like AI, a rambling AI answer full of slang looks human. No understanding of meaning. |

The stats signal is really three measurements:

- **Burstiness (weight 0.40).** Coefficient of variation of sentence lengths. People vary
  a lot; AI stays uniform. `cv ≤ 0.30` reads fully AI, `cv ≥ 0.80` fully human, straight
  line in between.
- **AI-favorite phrases (0.30).** Count hits of ~25 phrases models overuse ("furthermore,"
  "it is important to note," "delve into," and so on) per sentence.
- **Casual tells (0.30).** Contractions, lowercase "i," slang, stretched words, "!!!/…"
  per 100 words. These push the score toward human.
- Anything under 20 words gets pulled halfway to 0.5, because there isn't enough there to
  trust.

Blending them: `confidence = 0.65·llm + 0.35·style`. The LLM carries more weight because
it's the better judge of the fuzzy question. The stats are cheap, transparent backup. A
missing LLM means stats-only, never a silent 0.5.

## 2. What the confidence number means

`confidence` is my estimate of the odds the text is AI-written. 0 is clearly a person, 1 is
clearly a machine. It does not flip at 0.5 — it feeds three bands:

| confidence | call |
|---|---|
| `≥ 0.65` | `likely_ai` |
| `0.35 – 0.65` | `uncertain` |
| `< 0.35` | `likely_human` |

So a 0.60 means "leaning AI, but still inside the uncertain zone, so I won't say AI out
loud." The middle band is deliberately wide, taking up a third of the range, so borderline
and touched-up text lands on "not sure" instead of a false accusation. The raw signals are
already on a 0–1 scale (the LLM is told to save scores above 0.85 or below 0.15 for the
obvious cases), so the weighted blend stays on that same scale.

## 3. The three labels, word for word

**Looks AI (`likely_ai`):**
> 🤖 Likely AI-generated (AI-likelihood N%). Our automated analysis found strong,
> consistent signals of AI authorship. This is an automated assessment and can be wrong —
> if you wrote this yourself, you can contest it by filing an appeal with your content ID.

**Looks human (`likely_human`):**
> ✍️ Likely human-written (AI-likelihood N%, human-likelihood M%). Our signals are
> consistent with human authorship. Automated attribution is probabilistic, not proof of
> authorship.

**Can't tell (`uncertain`):**
> ❓ Uncertain (AI-likelihood N%). Our two signals disagreed or were inconclusive, so we
> are deliberately not labeling this as AI or human. When a definitive answer is needed, a
> human reviewer should make the call.

## 4. How appeals work

- **Who can appeal:** the writer — anyone holding the `content_id`.
- **What they send:** the `content_id` and a free-text `creator_reasoning` explaining
  themselves.
- **What the system does:** looks up the content, and if it exists, moves the status from
  `classified` to `under_review`, adds an appeal entry to the log with the original call,
  confidence, both signal scores, and the writer's note, then confirms receipt. It does not
  re-run the detector — that's a human's job on purpose.
- **What a reviewer sees:** `GET /appeals` gives them the queue of `under_review` items
  with the original text, scores, and call, so they can make the decision themselves.

## 5. Where it'll do badly

1. **Formal or non-native human writing.** Someone writing careful, formal English (like
   the monetary-policy sample) has low burstiness and no casual tells, so the stats signal
   reads them as AI (~0.70). The blend lands them in *uncertain* rather than a flat "AI,"
   but they'll never get confidently called human. This is exactly what the appeal path is
   for.
2. **AI that's been cleaned up.** Add slang, typos, and varied sentence lengths and you
   beat both signals at once — the stats see the casual markers, and the LLM's read
   softens. Expect false "human" or "uncertain" here.
3. **Really short text (under ~20 words).** Not enough sentences to measure burstiness. I
   damp the stats toward 0.5 and lean on the LLM, but the confidence is shaky no matter
   what.
4. **Poems, lists, code.** Repetition and short lines look AI-like on the stats even when a
   person obviously wrote them.

---

## Architecture

```
                          SUBMISSION FLOW
  ┌────────┐  {text, creator_id}   ┌──────────────────────────────┐
  │ client │ ────────────────────▶ │  POST /submit  (rate-limited) │
  └────────┘                       └──────────────┬───────────────┘
                                        raw text  │
                       ┌──────────────────────────┴───────────────┐
                       ▼                                           ▼
              ┌──────────────────┐                     ┌────────────────────┐
              │ Signal 1: LLM     │  ai_probability     │ Signal 2: stats     │
              │ (Groq llama-3.3)  │  0..1               │ burstiness+phrases  │
              └─────────┬────────┘                      │ +casual → 0..1      │
                        │  llm_score                     └──────────┬─────────┘
                        │                                style_score│
                        └───────────────┬───────────────────────────┘
                                        ▼
                          ┌──────────────────────────────┐
                          │ blend: 0.65·llm+0.35·style    │
                          │        → confidence 0..1       │
                          └───────────────┬───────────────┘
                                          │ confidence
                                          ▼
                          ┌──────────────────────────────┐
                          │ pick band + label text        │
                          └───────────────┬───────────────┘
                     content_id,          │ attribution, confidence,
                     scores, status       │ label
                                          ▼
                          ┌──────────────────┐        ┌──────────────────────┐
                          │ audit log (SQLite)│◀──────│ reply to client       │
                          │ event=submission  │        │ {content_id, label…} │
                          └──────────────────┘        └──────────────────────┘

                            APPEAL FLOW
  ┌────────┐ {content_id,        ┌──────────────┐  lookup   ┌──────────────────┐
  │ client │ creator_reasoning}  │ POST /appeal │ ────────▶ │ content store      │
  └────────┘ ──────────────────▶ └──────┬───────┘           │ status→under_review│
                                        │                    └─────────┬────────┘
                                        │ original scores + reasoning   │
                                        ▼                               ▼
                          ┌──────────────────┐            ┌──────────────────────┐
                          │ audit log (SQLite)│            │ reply: "received,     │
                          │ event=appeal      │            │ under_review"         │
                          └──────────────────┘            └──────────────────────┘
```

The submission flow: raw text fans out to both signals in `detection.analyze`, the two
0–1 scores get blended into one confidence, that number picks a band and its label text,
and the whole record is saved before the reply goes out with the `content_id` and label.
The appeal flow: the writer sends that `content_id` back with their note, the store flips
the record to `under_review` and logs an appeal entry with the original call plus the note,
and the API confirms it.

---

## The API

| Method | Path | Send | Get back |
|---|---|---|---|
| POST | `/submit` | `{text, creator_id}` | `content_id, attribution, confidence, label, signals{…}, status, timestamp` |
| POST | `/appeal` | `{content_id, creator_reasoning}` | `content_id, status, message, timestamp` (404 if the id is unknown) |
| GET | `/log` | `?limit=N` (≤500) | `{entries: [...]}`, newest first |
| GET | `/appeals` | — | `{queue: [...]}`, items under review |
| GET | `/health` | — | `{status: "ok"}` |

---

## AI tool plan

**M3 — submission endpoint + first signal.**
Give the AI section 1 (the signals) and the diagram. Ask it for the Flask skeleton with a
`POST /submit` stub and the LLM signal function. Check it by calling `llm_signal()` on a
couple of texts directly and making sure it returns a float in `[0,1]` shaped like the spec
says, before wiring it into the route.

**M4 — second signal + scoring.**
Give it section 1, section 2 (the confidence bands), and the diagram. Ask for the stats
function and the `combine()` scorer. Check it by running the four calibration inputs and
confirming the thresholds match section 2 (AI ≥ 0.65, human < 0.35) and that the scores
actually move. Don't accept scoring that looks reasonable but drifts off the specified
bands.

**M5 — production layer.**
Give it section 3 (labels), section 4 (appeals), and the diagram. Ask for `make_label()`
and the `/appeal` endpoint. Check it by generating all three labels and diffing them
against section 3, then running submit → appeal → `GET /log` and confirming the status
flips to `under_review` with the reasoning saved.
