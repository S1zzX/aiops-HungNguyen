#!/usr/bin/env bash
set -euo pipefail

SERVICE=""
REPLICAS=2
DRY_RUN=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../configs/docker-compose.yml"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)   SERVICE="$2";   shift 2 ;;
    --replicas)  REPLICAS="$2";  shift 2 ;;
    --dry-run)   DRY_RUN=true;   shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$SERVICE" ]]; then
  echo "[scale_replicas] ERROR: --service required"
  exit 1
fi

if $DRY_RUN; then
  echo "[DRY-RUN] would execute: docker compose scale ${SERVICE}=${REPLICAS}"
  exit 0
fi

echo "[scale_replicas] Scaling $SERVICE to $REPLICAS replicas..."
docker compose -f "$COMPOSE_FILE" up -d --scale "${SERVICE}=${REPLICAS}" --no-recreate
echo "[scale_replicas] Done."
exit 0