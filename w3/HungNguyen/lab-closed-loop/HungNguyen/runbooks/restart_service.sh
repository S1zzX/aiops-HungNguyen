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
  echo "[restart_service] ERROR: --service required"
  exit 1
fi

CONTAINER="ronki-${SERVICE}"

if $DRY_RUN; then
  echo "[DRY-RUN] would execute: docker restart $CONTAINER"
  exit 0
fi

echo "[restart_service] Restarting $CONTAINER..."
if ! docker inspect "$CONTAINER" > /dev/null 2>&1; then
  docker start "$CONTAINER"
else
  docker restart "$CONTAINER"
fi

sleep 5
STATUS=$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")
if [[ "$STATUS" == "running" ]]; then
  echo "[restart_service] $CONTAINER is running."
  exit 0
else
  echo "[restart_service] ERROR: $CONTAINER status=$STATUS"
  exit 1
fi