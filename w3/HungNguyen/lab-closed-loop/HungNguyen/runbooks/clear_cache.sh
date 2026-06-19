#!/usr/bin/env bash
set -euo pipefail

SERVICE=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)  SERVICE="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=true; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$SERVICE" ]]; then
  echo "[clear_cache] ERROR: --service required"
  exit 1
fi

CONTAINER="ronki-${SERVICE}"

if $DRY_RUN; then
  echo "[DRY-RUN] would execute: docker kill --signal=SIGHUP $CONTAINER"
  exit 0
fi

if ! docker inspect "$CONTAINER" > /dev/null 2>&1; then
  echo "[clear_cache] ERROR: container $CONTAINER not found."
  exit 1
fi

echo "[clear_cache] Sending SIGHUP to $CONTAINER..."
docker kill --signal=SIGHUP "$CONTAINER" || true
sleep 2

STATUS=$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")
if [[ "$STATUS" != "running" ]]; then
  echo "[clear_cache] WARNING: $CONTAINER stopped after SIGHUP (no handler in mock service) — restarting to self-heal..."
  docker start "$CONTAINER"
  sleep 3
  STATUS=$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")
fi

if [[ "$STATUS" == "running" ]]; then
  echo "[clear_cache] Cache flush triggered, $CONTAINER is running."
  exit 0
else
  echo "[clear_cache] ERROR: $CONTAINER status=$STATUS after self-heal attempt"
  exit 1
fi