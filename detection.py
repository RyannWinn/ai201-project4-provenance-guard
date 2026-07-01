"""
detection.py — Provenance Guard detection pipeline.

Two independent signals + a calibrated combiner:

  Signal 1  (llm_score)   : an LLM (Groq / Llama-3.1) judges how AI-generated the
                            text reads, on a 0..1 scale. Captures holistic phrasing,
                            structure and "voice" that simple statistics miss.
  Signal 2  (style_score) : deterministic stylometric heuristics, 0..1. Captures
                            measurable surface properties (sentence-length variance,
                            AI-hallmark connective density, informality markers).

The combined confidence is the estimated probability the text is AI-generated,
where 0.0 = clearly human and 1.0 = clearly AI. Thresholds map that number to one
of three attributions so the system never does a hard binary flip at 0.5.
"""

from __future__ import annotations

import json
import os
import re
import statistics
from dataclasses import dataclass, asdict

# --- tunables (kept in one place so they match planning.md) --------------------

LLM_WEIGHT = 0.65          # LLM is the stronger, more holistic signal
STYLE_WEIGHT = 0.35        # stylometrics are cheap but noisier

AI_THRESHOLD = 0.65        # >= this  -> "likely_ai"
HUMAN_THRESHOLD = 0.35     # <  this  -> "likely_human"; between the two -> "uncertain"

GROQ_MODEL = "llama-3.3-70b-versatile"

# AI writing very often over-uses these formal connectives / hedges.
_AI_HALLMARK_PHRASES = [
    "furthermore", "moreover", "additionally", "in addition", "however",
    "it is important to note", "it is worth noting", "in conclusion",
    "in summary", "overall", "as a result", "consequently", "therefore",
    "on the other hand", "in today's world", "in the modern era",
    "plays a crucial role", "plays a vital role", "a myriad of",
    "delve into", "navigate the", "it is essential", "paradigm",
    "landscape of", "realm of", "underscores", "multifaceted",
]

# Markers that strongly suggest a casual human wrote it.
_INFORMAL_MARKERS = [
    r"\bi\b",                 # lowercase 'i' as a pronoun
    r"\bok\b|\bokay\b|\byeah\b|\bnah\b|\blol\b|\bhonestly\b|\bkinda\b|\bgonna\b|\bwanna\b",
    r"(\w)\1{2,}",            # elongations like "sooo" / "yesss"
    r"\.\.\.|!!+|\?\?+",      # informal punctuation runs
    r"\b(?:can't|won't|don't|didn't|i'm|i've|it's|that's|there's)\b",  # contractions
]


@dataclass
class SignalResult:
    llm_score: float
    style_score: float
    confidence: float
    attribution: str
    llm_reasoning: str
    style_detail: dict
    llm_available: bool


# ------------------------------------------------------------------------------
# Signal 2 — stylometric heuristics (deterministic, no network)
# ------------------------------------------------------------------------------

def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"[.!?]+", text)
    return [p.strip() for p in parts if p.strip()]


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z']+", text.lower())


def stylometric_signal(text: str) -> tuple[float, dict]:
    """Return (style_score in 0..1, detail dict). Higher => more AI-like."""
    words = _tokenize(text)
    sentences = _split_sentences(text)
    n_words = len(words)
    n_sent = len(sentences)

    detail: dict = {}

    # --- Metric A: sentence-length burstiness ---------------------------------
    # Humans vary sentence length a lot (high coefficient of variation);
    # AI trends toward uniform, medium-length sentences (low CV).
    sent_lengths = [len(_tokenize(s)) for s in sentences] or [n_words]
    mean_len = statistics.mean(sent_lengths) if sent_lengths else 0.0
    if len(sent_lengths) >= 2 and mean_len > 0:
        cv = statistics.pstdev(sent_lengths) / mean_len
    else:
        cv = 0.5  # not enough sentences to judge -> neutral-ish
    # cv <= 0.30 reads very AI; cv >= 0.80 reads very human.
    ai_burstiness = _clamp((0.80 - cv) / (0.80 - 0.30))
    detail["sentence_cv"] = round(cv, 3)
    detail["ai_burstiness"] = round(ai_burstiness, 3)

    # --- Metric B: AI-hallmark connective density ----------------------------
    low = " " + text.lower() + " "
    hallmarks = sum(low.count(p) for p in _AI_HALLMARK_PHRASES)
    density = hallmarks / max(n_sent, 1)
    # ~0.5 hallmarks/sentence is already very heavy.
    ai_connectives = _clamp(density / 0.50)
    detail["hallmark_hits"] = hallmarks
    detail["hallmark_density"] = round(density, 3)
    detail["ai_connectives"] = round(ai_connectives, 3)

    # --- Metric C: informality (pushes toward human) -------------------------
    informal_hits = 0
    for pat in _INFORMAL_MARKERS:
        informal_hits += len(re.findall(pat, text.lower()))
    # normalise per 100 words
    informality = informal_hits / max(n_words, 1) * 100.0
    # 0 markers/100w reads AI; >=6 markers/100w reads clearly human.
    ai_informality = _clamp(1.0 - informality / 6.0)
    detail["informal_hits"] = informal_hits
    detail["informality_per_100w"] = round(informality, 2)
    detail["ai_informality"] = round(ai_informality, 3)

    style_score = 0.40 * ai_burstiness + 0.30 * ai_connectives + 0.30 * ai_informality

    # Very short texts are unreliable -> pull toward the uncertain middle.
    if n_words < 20:
        style_score = 0.5 * style_score + 0.5 * 0.5
        detail["short_text_dampened"] = True

    detail["n_words"] = n_words
    detail["n_sentences"] = n_sent
    return round(_clamp(style_score), 4), detail


# ------------------------------------------------------------------------------
# Signal 1 — LLM judge (Groq)
# ------------------------------------------------------------------------------

_LLM_SYSTEM = (
    "You are a forensic text-attribution assistant. Given a passage, estimate the "
    "probability that it was generated by an AI language model rather than written "
    "by a human. Consider tone, structure, predictability, hedging, and formulaic "
    "phrasing. You are one signal in a larger system and must be calibrated: reserve "
    "scores above 0.85 or below 0.15 for clear-cut cases. Respond with ONLY a single "
    "line of raw JSON (no markdown, no code fence, no newlines inside strings) of the "
    'form {"ai_probability": <float 0..1>, "reasoning": "<one short sentence>"}.'
)


def llm_signal(text: str) -> tuple[float | None, str]:
    """Return (ai_probability 0..1 or None if unavailable, reasoning str)."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None, "LLM signal skipped: GROQ_API_KEY not set."
    try:
        from groq import Groq

        client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user", "content": f"Passage:\n\"\"\"\n{text}\n\"\"\""},
            ],
            temperature=0.0,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content or "{}"
        try:
            data = json.loads(raw)
            score = float(data["ai_probability"])
            reasoning = str(data.get("reasoning", "")).strip()
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            # Salvage a malformed-but-recoverable response (e.g. an unescaped
            # newline inside the reasoning string) by regexing out the number.
            m = re.search(r'"ai_probability"\s*:\s*([0-9]*\.?[0-9]+)', raw)
            if not m:
                raise
            score = float(m.group(1))
            rm = re.search(r'"reasoning"\s*:\s*"([^"]+)', raw)
            reasoning = rm.group(1).strip() if rm else ""
        return _clamp(score), reasoning or "(no reasoning returned)"
    except Exception as exc:  # network error, bad JSON, missing field, etc.
        return None, f"LLM signal error: {exc.__class__.__name__}: {exc}"


# ------------------------------------------------------------------------------
# Combiner
# ------------------------------------------------------------------------------

def combine(llm_score: float | None, style_score: float) -> tuple[float, bool]:
    """Blend the two signals into a single 0..1 confidence (P(AI-generated)).

    If the LLM signal is unavailable we fall back to the stylometric signal alone
    rather than silently treating a missing signal as 0.5.
    """
    if llm_score is None:
        return round(style_score, 4), False
    confidence = LLM_WEIGHT * llm_score + STYLE_WEIGHT * style_score
    return round(_clamp(confidence), 4), True


def attribution_for(confidence: float) -> str:
    if confidence >= AI_THRESHOLD:
        return "likely_ai"
    if confidence < HUMAN_THRESHOLD:
        return "likely_human"
    return "uncertain"


def analyze(text: str) -> SignalResult:
    style_score, style_detail = stylometric_signal(text)
    llm_score, llm_reasoning = llm_signal(text)
    confidence, llm_available = combine(llm_score, style_score)
    attribution = attribution_for(confidence)
    return SignalResult(
        llm_score=llm_score if llm_score is not None else -1.0,
        style_score=style_score,
        confidence=confidence,
        attribution=attribution,
        llm_reasoning=llm_reasoning,
        style_detail=style_detail,
        llm_available=llm_available,
    )


# ------------------------------------------------------------------------------
# Transparency labels — exact user-facing text for each attribution
# ------------------------------------------------------------------------------

def make_label(attribution: str, confidence: float) -> str:
    pct = round(confidence * 100)
    human_pct = 100 - pct
    if attribution == "likely_ai":
        return (
            f"🤖 Likely AI-generated (AI-likelihood {pct}%). "
            "Our automated analysis found strong, consistent signals of AI authorship. "
            "This is an automated assessment and can be wrong — if you wrote this "
            "yourself, you can contest it by filing an appeal with your content ID."
        )
    if attribution == "likely_human":
        return (
            f"✍️ Likely human-written (AI-likelihood {pct}%, human-likelihood {human_pct}%). "
            "Our signals are consistent with human authorship. Automated attribution is "
            "probabilistic, not proof of authorship."
        )
    return (
        f"❓ Uncertain (AI-likelihood {pct}%). "
        "Our two signals disagreed or were inconclusive, so we are deliberately not "
        "labeling this as AI or human. When a definitive answer is needed, a human "
        "reviewer should make the call."
    )


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def result_to_dict(r: SignalResult) -> dict:
    d = asdict(r)
    d["llm_score"] = None if r.llm_score < 0 else r.llm_score
    return d
