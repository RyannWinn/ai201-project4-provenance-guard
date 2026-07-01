#!/usr/bin/env bash
# Demo script for the video walkthrough.
#
# First, in another terminal, start the server:
#     .venv/bin/python app.py
#
# Then run this:
#     bash demo.sh
#
# It submits a human sample, an AI sample, appeals the human one, and prints the log.
# The content_id for the appeal is captured automatically, so you don't copy/paste it.

# Note: use 127.0.0.1, not localhost. On macOS, "localhost" resolves to IPv6 first,
# where the built-in AirPlay Receiver squats on port 5000 and eats the request.
# If port 5000 is taken by AirPlay, start the app with `PORT=5001 python app.py`
# and run this as `PORT=5001 bash demo.sh`.
set -e
PORT="${PORT:-5000}"
BASE="http://127.0.0.1:${PORT}"

echo "============================================================"
echo "1. SUBMIT — casual human text (expect: likely_human, low score)"
echo "============================================================"
HUMAN=$(curl -s -X POST "$BASE/submit" -H "Content-Type: application/json" \
  -d '{"text":"ok so i finally tried that new ramen place downtown and honestly? underwhelming. the broth was fine but they put WAY too much sodium in it and i was thirsty for hours after. probably wont go back","creator_id":"demo"}')
echo "$HUMAN" | python -m json.tool
CID=$(echo "$HUMAN" | python -c "import sys,json;print(json.load(sys.stdin)['content_id'])")
echo ""
echo ">> saved content_id for the appeal: $CID"
echo ""

echo "============================================================"
echo "2. SUBMIT — stiff AI text (expect: likely_ai, high score)"
echo "============================================================"
curl -s -X POST "$BASE/submit" -H "Content-Type: application/json" \
  -d '{"text":"Artificial intelligence represents a transformative paradigm shift in modern society. It is important to note that while the benefits are numerous, it is equally essential to consider the ethical implications. Furthermore, stakeholders across various sectors must collaborate to ensure responsible deployment.","creator_id":"demo"}' \
  | python -m json.tool
echo ""

echo "============================================================"
echo "3. APPEAL — contest the human submission"
echo "============================================================"
curl -s -X POST "$BASE/appeal" -H "Content-Type: application/json" \
  -d "{\"content_id\":\"$CID\",\"creator_reasoning\":\"I wrote this myself, it is a real review from personal experience\"}" \
  | python -m json.tool
echo ""

echo "============================================================"
echo "4. LOG — the audit trail (note the appeal flips status)"
echo "============================================================"
curl -s "$BASE/log" | python -m json.tool
