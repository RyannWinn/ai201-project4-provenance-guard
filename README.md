# Provenance Guard

You give it a chunk of text. It guesses whether a human or an AI wrote it, tells you how
sure it is in plain language, writes the decision down, and lets you push back if it got
you wrong.

Under the hood there are two detectors that look at the text in completely different ways.
Their scores get blended into one number, that number picks a label, and every submission
and appeal lands in an audit log. Submissions are rate-limited so nobody can hammer it.

The reasoning behind all the design choices is in [planning.md](planning.md). This file is
about what actually got built and why it ended up the way it did.

## Running it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # paste your Groq key in here
.venv/bin/python app.py       # http://127.0.0.1:5000
```

Try it:

```bash
curl -s -X POST http://localhost:5000/submit \
  -H "Content-Type: application/json" \
  -d '{"text": "The sun dipped below the horizon...", "creator_id": "test-user-1"}' \
  | python -m json.tool
```

## How it fits together

Two paths through the system. The full diagram and a step-by-step walk are in
[planning.md → Architecture](planning.md#architecture). The short version:

```
/submit ─▶ Signal 1 (LLM) ─┐
                            ├─▶ blend (0.65·llm + 0.35·style) ─▶ pick a band ─▶ label ─▶ log ─▶ reply
/submit ─▶ Signal 2 (style)─┘

/appeal ─▶ mark under_review ─▶ log the appeal ─▶ reply
```

The endpoints:

| Method | Path | Send | Get back |
|---|---|---|---|
| POST | `/submit` | `{text, creator_id}` | `content_id, attribution, confidence, label, signals, status, timestamp` |
| POST | `/appeal` | `{content_id, creator_reasoning}` | `content_id, status, message, timestamp` |
| GET | `/log` | `?limit=N` | `{entries: [...]}`, newest first |
| GET | `/appeals` | — | `{queue: [...]}`, whatever's waiting on a reviewer |
| GET | `/health` | — | `{status: "ok"}` |

Where things live: [app.py](app.py) has the routes and rate limiting,
[detection.py](detection.py) has both signals plus the scoring and labels, and
[store.py](store.py) handles the SQLite storage and the audit log.

## The two signals, and why I picked them

**Signal 1 is an LLM.** The text goes to Groq's `llama-3.3-70b-versatile`, which reads it
and returns a probability that it's AI-written plus a one-line reason. I leaned on a model
here because "does this sound like a machine wrote it" is exactly the kind of fuzzy,
whole-passage judgment that a big model is good at and that no single statistic gets right.
It's the better of the two signals, so it gets the heavier weight.

**Signal 2 is plain statistics** — no network, fully inspectable. It's three measurements
mashed into one score:

- **Burstiness (40%).** People write sentences of wildly different lengths. AI tends to
  churn out sentences that are all about the same length. So I measure how much the
  sentence lengths vary. Low variation looks like a machine.
- **AI-favorite phrases (30%).** Models lean on a handful of tics — "furthermore," "it is
  important to note," "delve into," and a couple dozen others. I count how often they show
  up per sentence.
- **Casual tells (30%).** Contractions, a lowercase "i," slang, "sooo," "!!!" — these
  scream human, so they pull the score down.

Why two detectors of such different kinds? The LLM is a black box. If it says "AI" I can't
really see why. The stats signal is the opposite: dead simple, free, and I can point at the
exact numbers. Having both means I get a second opinion, and when the two disagree that
disagreement is useful on its own — it's what shoves a piece of text into the "not sure"
pile.

### How the scoring works

`confidence = 0.65·llm_score + 0.35·style_score`. That number is my estimate of the odds
the text is AI-written, 0 meaning "clearly a person," 1 meaning "clearly a machine." If the
Groq call falls over, I drop back to the stats score alone instead of pretending a missing
signal is a neutral 0.5.

The important part: it doesn't just flip at 0.5. There are three bands.

| confidence | call |
|---|---|
| `≥ 0.65` | **likely_ai** |
| `0.35 – 0.65` | **uncertain** |
| `< 0.35` | **likely_human** |

I made the middle band wide on purpose. Accusing a real writer of using AI is the worst
mistake this system can make, so anything close to the fence gets a shrug instead of a
verdict.

### Proof the score actually moves

Two real runs, both signals live:

**Reads as AI** — `confidence 0.86` → `likely_ai`
> *"Artificial intelligence represents a transformative paradigm shift in modern society.
> It is important to note that while the benefits are numerous, it is equally essential to
> consider the ethical implications. Furthermore, stakeholders across various sectors must
> collaborate…"*
> `llm=0.80`, `style=0.98` — flat sentence lengths, 3 of the AI tic-phrases, zero casual
> markers.

**Reads as human** — `confidence 0.18` → `likely_human`
> *"ok so i finally tried that new ramen place downtown and honestly? underwhelming. the
> broth was fine but they put WAY too much sodium in it…"*
> `llm=0.21`, `style=0.12` — sentences all over the place, no tic-phrases, tons of casual
> markers.

And the full four-input check I ran to calibrate it:

| Input | llm | style | confidence | call |
|---|---|---|---|---|
| Obviously AI | 0.82 | 0.94 | **0.86** | likely_ai |
| Obviously human | 0.21 | 0.15 | **0.19** | likely_human |
| Formal human (econ paper) | 0.62 | 0.70 | **0.65** | uncertain |
| AI someone lightly edited | 0.42 | 0.38 | **0.41** | uncertain |

## The three labels

The text `/submit` hands back changes depending on the band (see `make_label` in
[detection.py](detection.py)). `N` and `M` get filled in with the actual percentages.

**When it looks AI (`likely_ai`):**
> 🤖 Likely AI-generated (AI-likelihood N%). Our automated analysis found strong,
> consistent signals of AI authorship. This is an automated assessment and can be wrong —
> if you wrote this yourself, you can contest it by filing an appeal with your content ID.

**When it looks human (`likely_human`):**
> ✍️ Likely human-written (AI-likelihood N%, human-likelihood M%). Our signals are
> consistent with human authorship. Automated attribution is probabilistic, not proof of
> authorship.

**When it can't tell (`uncertain`):**
> ❓ Uncertain (AI-likelihood N%). Our two signals disagreed or were inconclusive, so we
> are deliberately not labeling this as AI or human. When a definitive answer is needed, a
> human reviewer should make the call.

## Appeals

If you think the call was wrong, send back your `content_id` and a note explaining
yourself to `/appeal`. The system flips that item to `under_review`, drops an appeal entry
into the log (with your note and the original scores attached), and confirms it got the
message. It does not re-run the detector — a person is supposed to look at it. Reviewers
pull the waiting items from `GET /appeals`.

```bash
curl -s -X POST http://localhost:5000/appeal \
  -H "Content-Type: application/json" \
  -d '{"content_id": "PASTE-CONTENT-ID", "creator_reasoning": "I wrote this myself; English is my second language so it reads formal."}' \
  | python -m json.tool
```

## Rate limiting

`/submit` allows **10 requests a minute and 100 a day per IP**, via Flask-Limiter with an
in-memory store. The thinking: someone checking their own drafts might hit it a few times
in a row, and 10/min covers that easily. The daily cap is there to stop a script from
scraping the classifier or quietly burning through Groq credits. The read-only endpoints
aren't capped, so appeals and the log are always reachable.

Here's it kicking in — 12 fast requests against the 10/min limit:

```
200
200
200
200
200
200
200
200
200
200
429   <- cut off here
429
```

## The audit log

Every submission and every appeal writes a row to SQLite ([store.py](store.py), the
`audit` table): timestamp, content ID, who sent it, the call, the combined confidence,
**both individual signal scores**, the status, and the appeal note if there is one. A real
slice from `GET /log` — three submissions covering all three calls, plus one appeal:

```json
{
  "entries": [
    { "id": 4, "event": "appeal", "content_id": "fffd247c-…", "creator_id": "carol",
      "attribution": "uncertain", "confidence": 0.648, "llm_score": 0.62,
      "style_score": 0.7, "status": "under_review",
      "appeal_reasoning": "I am an economist and wrote this myself; my academic style is formal but human.",
      "timestamp": "2026-07-01T05:20:17.351Z" },
    { "id": 3, "event": "submission", "content_id": "fffd247c-…", "creator_id": "carol",
      "attribution": "uncertain", "confidence": 0.648, "llm_score": 0.62,
      "style_score": 0.7, "status": "classified", "appeal_reasoning": null,
      "timestamp": "2026-07-01T05:20:17.323Z" },
    { "id": 2, "event": "submission", "content_id": "d661cb28-…", "creator_id": "bob",
      "attribution": "likely_human", "confidence": 0.1791, "llm_score": 0.21,
      "style_score": 0.1218, "status": "classified", "appeal_reasoning": null,
      "timestamp": "2026-07-01T05:20:16.770Z" },
    { "id": 1, "event": "submission", "content_id": "75f323a9-…", "creator_id": "alice",
      "attribution": "likely_ai", "confidence": 0.8619, "llm_score": 0.8,
      "style_score": 0.9768, "status": "classified", "appeal_reasoning": null,
      "timestamp": "2026-07-01T05:20:16.025Z" }
  ]
}
```

Look at entries 3 and 4: same content, going from `classified` to `under_review` once the
appeal comes in, with the reasoning saved alongside the original scores.

## Where it falls down

- **Formal or non-native writing gets punished.** The stats signal is built around
  burstiness and casual tells, and careful academic or second-language writing has neither.
  The econ sample above scored `style=0.70` and only dodged a false "AI" verdict because
  the wide uncertain band caught it. It'll basically never get confidently called human.
  That's the whole reason appeals exist.
- **AI that's been touched up beats it.** Sprinkle in some slang, a few contractions, and
  uneven sentence lengths, and you raise the casual-tell and burstiness numbers *and*
  soften the LLM's read. It drifts toward human or uncertain. This is a cat-and-mouse
  problem and I don't pretend to have solved it.
- **Short text is basically a coin flip.** Under ~20 words there aren't enough sentences to
  measure burstiness, so I damp the stats score toward 0.5 and the confidence isn't worth
  much.
- **Poems, lists, and code confuse it.** Repetition and short lines read as AI-like on the
  stats even when a person clearly wrote them.

## What the spec did for me

Writing [planning.md](planning.md) before touching code paid off in two concrete spots.
Because I'd already written out the three bands and the exact label wording, turning them
into `attribution_for` and `make_label` was almost copy-paste. And the four-input
calibration test had a real target to hit (AI at or above 0.65, human below 0.35) instead
of me eyeballing the output and calling it "close enough."

Where the build drifted from the plan: I'd written down `llama-3.1-8b-instant` as the LLM.
When I tested it, the 8B model returned roughly 0.32 for *everything* — it couldn't tell
the obviously-AI sample apart from the obviously-human one. I moved up to
`llama-3.3-70b-versatile` and got the spread you see above. I'd also planned to use Groq's
strict JSON mode; it kept rejecting perfectly reasonable answers over an unescaped newline,
so I ditched it and parse the response myself with a regex fallback.

## Where I used AI

- I had it generate the first cut of the detection module and the Flask skeleton straight
  from the planning doc. I overrode two things: the model (bumped 8B → 70B after the 8B
  couldn't separate the classes), and the strict-JSON setting, which I swapped for
  tolerant parsing after it threw 400s on valid-looking output.
- I had it write the stylometric metrics. Its first version had a broken regex for
  detecting stretched-out words (`[a-z]{2,}(\1)\1+`, a backreference to a group that
  doesn't exist) that crashed on every call — I rewrote it as `(\w)\1{2,}`. I also added
  the short-text damping it left out, since burstiness means nothing on one sentence.

## Walkthrough

_Recorded separately. A couple of minutes running `/submit` → label → `/appeal` → `/log`
end to end, talking through why there are two signals and why the middle band is so wide._
