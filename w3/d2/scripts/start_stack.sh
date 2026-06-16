#!/usr/bin/env bash
# STUB — wire to your docker-compose. Pack does not ship a stack.
set -e
echo "=== start_stack.sh STUB ==="
echo "This pack does NOT ship docker-compose.yml. Wire your own stack here."
echo ""
echo "Expected behavior:"
echo "  1. docker compose up -d (your 10-service stack)"
echo "  2. wait for all healthchecks pass"
echo "  3. wait for AIOps pipeline /alerts endpoint to respond 200"
echo ""
echo "Example:"
echo "  docker compose up -d"
echo "  timeout 120 bash -c 'until curl -sf http://localhost:8000/alerts?since=0 >/dev/null; do sleep 2; done'"
echo "  echo stack ready"
exit 1
