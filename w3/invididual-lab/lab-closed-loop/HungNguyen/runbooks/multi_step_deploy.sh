#!/usr/bin/env bash
# multi_step_deploy.sh — transactional multi-step deploy runbook
# Supports: --step a | b | c | rollback-a | rollback-b
# Used by Scenario 4 (transactional rollback chain).
set -euo pipefail

SERVICE=""
STEP=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service) SERVICE="$2"; shift 2 ;;
    --step)    STEP="$2";    shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$SERVICE" ]]; then
  echo "[multi_step_deploy] ERROR: --service required"
  exit 1
fi

if [[ -z "$STEP" ]]; then
  echo "[multi_step_deploy] ERROR: --step required (a|b|c|rollback-a|rollback-b)"
  exit 1
fi

CONTAINER="ronki-${SERVICE}"

if $DRY_RUN; then
  echo "[DRY-RUN] would execute: multi_step_deploy --step ${STEP} on ${CONTAINER}"
  exit 0
fi

case "$STEP" in
  a)
    echo "[multi_step_deploy] Step A: validating image config for $CONTAINER..."
    if ! docker inspect "$CONTAINER" > /dev/null 2>&1; then
      echo "[multi_step_deploy] ERROR: $CONTAINER not found"
      exit 1
    fi
    echo "[multi_step_deploy] Step A complete."
    ;;

  b)
    echo "[multi_step_deploy] Step B: applying new config labels to $CONTAINER..."
    if ! docker inspect "$CONTAINER" > /dev/null 2>&1; then
      echo "[multi_step_deploy] ERROR: $CONTAINER not found"
      exit 1
    fi
    echo "[multi_step_deploy] Step B complete."
    ;;

  c)
    # Step C: final cutover — FAILS if container is not running (simulates real failure)
    echo "[multi_step_deploy] Step C: final traffic cutover for $CONTAINER..."
    STATUS=$(docker inspect --format '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "missing")
    if [[ "$STATUS" != "running" ]]; then
      echo "[multi_step_deploy] ERROR: Step C failed — $CONTAINER not running (status=$STATUS)"
      exit 1
    fi
    echo "[multi_step_deploy] Step C complete."
    ;;

  rollback-a)
    echo "[multi_step_deploy] Rollback A: restoring pre-deploy state for $CONTAINER..."
    docker inspect "$CONTAINER" > /dev/null 2>&1 \
      && docker restart "$CONTAINER" \
      || docker start "$CONTAINER" 2>/dev/null || true
    sleep 3
    echo "[multi_step_deploy] Rollback A complete."
    ;;

  rollback-b)
    echo "[multi_step_deploy] Rollback B: reverting config changes for $CONTAINER..."
    docker inspect "$CONTAINER" > /dev/null 2>&1 \
      && docker restart "$CONTAINER" \
      || docker start "$CONTAINER" 2>/dev/null || true
    sleep 3
    echo "[multi_step_deploy] Rollback B complete."
    ;;

  *)
    echo "[multi_step_deploy] ERROR: unknown step '$STEP'. Valid: a|b|c|rollback-a|rollback-b"
    exit 1
    ;;
esac
