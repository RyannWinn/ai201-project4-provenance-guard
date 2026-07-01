"""
app.py — Provenance Guard Flask API.

Endpoints
  POST /submit   {text, creator_id}                 -> classify + label + audit
  POST /appeal   {content_id, creator_reasoning}    -> flip status, log appeal
  GET  /log      ?limit=N                            -> recent audit entries
  GET  /appeals                                      -> the human-reviewer queue
  GET  /health                                       -> liveness probe
"""

from __future__ import annotations

import os
import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import detection
import store

load_dotenv()

app = Flask(__name__)
store.init_db()

# In-memory storage is fine for local dev / grading. See README for the chosen
# limits and the reasoning behind them.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)


@app.post("/submit")
@limiter.limit("10 per minute;100 per day")
def submit():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    creator_id = (data.get("creator_id") or "").strip()

    if not text:
        return jsonify({"error": "field 'text' is required and must be non-empty"}), 400
    if not creator_id:
        return jsonify({"error": "field 'creator_id' is required"}), 400

    content_id = str(uuid.uuid4())
    result = detection.analyze(text)
    label = detection.make_label(result.attribution, result.confidence)
    meta = store.record_submission(content_id, creator_id, text, result)

    return jsonify({
        "content_id": content_id,
        "creator_id": creator_id,
        "attribution": result.attribution,
        "confidence": result.confidence,
        "label": label,
        "signals": {
            "llm_score": None if result.llm_score < 0 else result.llm_score,
            "style_score": result.style_score,
            "llm_available": result.llm_available,
            "llm_reasoning": result.llm_reasoning,
            "style_detail": result.style_detail,
        },
        "status": meta["status"],
        "timestamp": meta["timestamp"],
    })


@app.post("/appeal")
def appeal():
    data = request.get_json(silent=True) or {}
    content_id = (data.get("content_id") or "").strip()
    reasoning = (data.get("creator_reasoning") or "").strip()

    if not content_id:
        return jsonify({"error": "field 'content_id' is required"}), 400
    if not reasoning:
        return jsonify({"error": "field 'creator_reasoning' is required"}), 400

    outcome = store.record_appeal(content_id, reasoning)
    if outcome is None:
        return jsonify({"error": f"unknown content_id: {content_id}"}), 404

    return jsonify({
        "content_id": content_id,
        "status": outcome["status"],
        "message": (
            "Appeal received. This content is now marked 'under_review' and has been "
            "logged for a human reviewer. No automated re-classification is performed."
        ),
        "timestamp": outcome["timestamp"],
    })


@app.get("/log")
def log():
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 500)
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"entries": store.get_log(limit)})


@app.get("/appeals")
def appeals_queue():
    return jsonify({"queue": store.get_appeal_queue()})


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    # macOS runs AirPlay Receiver on port 5000, which blocks Flask from binding.
    # Override with e.g. `PORT=5001 python app.py`, or turn AirPlay Receiver off.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="127.0.0.1", port=port, debug=True)
