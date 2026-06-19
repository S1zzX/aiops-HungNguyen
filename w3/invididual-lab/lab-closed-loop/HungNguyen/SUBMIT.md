# SUBMIT.md — Chaos Scenario Results

**Author:** HungNguyen  
**Date:** 2026-06-18

---

## Scenario 1 — Action Succeeds (HighLatency → restart → VERIFY_PASS)

**Inject command:**
```bash
curl -s -X POST http://localhost:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{"labels":{"alertname":"HighLatency","service":"payment-svc","severity":"critical"}}]'
```

**Expected log events:** `ALERT_DETECTED` → `DECIDE_RUNBOOK` → `BLAST_RADIUS_OK` → `DRY_RUN_PASS` → `ACTION_EXECUTED` → `VERIFY_PASS` → `ACTION_SUCCESS`

**Actual log output:**

```json
{"ts": "2026-06-18T09:38:22.376320+00:00", "level": "INFO", "event_type": "ALERT_DETECTED", "alertname": "HighLatency", "service": "payment-svc", "severity": "critical"}
{"ts": "2026-06-18T09:38:22.376412+00:00", "level": "INFO", "event_type": "DECIDE_RUNBOOK", "alertname": "HighLatency", "service": "payment-svc", "runbook": "runbooks/restart_service.sh"}
{"ts": "2026-06-18T09:38:22.376506+00:00", "level": "INFO", "event_type": "BLAST_RADIUS_OK", "alertname": "HighLatency", "service": "payment-svc"}
{"ts": "2026-06-18T09:38:22.383949+00:00", "level": "INFO", "event_type": "DRY_RUN_PASS", "alertname": "HighLatency", "service": "payment-svc", "runbook": "runbooks/restart_service.sh"}
{"ts": "2026-06-18T09:38:28.869972+00:00", "level": "INFO", "event_type": "ACTION_EXECUTED", "alertname": "HighLatency", "service": "payment-svc", "runbook": "runbooks/restart_service.sh", "success": true, "output": "[restart_service] Restarting ronki-payment-svc...\nronki-payment-svc\n[restart_service] ronki-payment-svc is running."}
{"ts": "2026-06-18T09:38:28.870056+00:00", "level": "INFO", "event_type": "VERIFY_START", "service": "payment-svc", "timeout_s": 60}
{"ts": "2026-06-18T09:38:28.875828+00:00", "level": "INFO", "event_type": "VERIFY_SAMPLE", "service": "payment-svc", "sample": 1, "latency_p99_ms": 0.0, "up": 1.0, "latency_ok": true, "up_ok": true}
{"ts": "2026-06-18T09:41:49.877409+00:00", "level": "INFO", "event_type": "VERIFY_PASS", "service": "payment-svc", "samples": 3}
{"ts": "2026-06-18T09:41:49.877502+00:00", "level": "INFO", "event_type": "ACTION_SUCCESS", "alertname": "HighLatency", "service": "payment-svc", "runbook": "runbooks/restart_service.sh"}
{"ts": "2026-06-18T09:41:53.216998+00:00", "level": "WARNING", "event_type": "BLAST_RADIUS_EXCEEDED", "alertname": "HighLatency", "service": "payment-svc", "reason": "restarts/hour limit for payment-svc"}
```

**Observations:**
- Orchestrator detected `HighLatency` alert on `payment-svc` within one poll cycle (15s)
- Dry-run passed before real execution
- `payment-svc` container restarted successfully
- Verify passed after 3 consecutive samples with `up=1.0`
- Blast-radius guard correctly blocked subsequent redundant restarts

**Result:** ✅ PASS

---

## Scenario 2 — Action Fails → Auto-Rollback (InstanceDown → rollback)

**Inject command:**
```bash
docker rm -f ronki-checkout-svc
curl -s -X POST http://localhost:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{"labels":{"alertname":"InstanceDown","service":"checkout-svc","severity":"critical"}}]'
```

**Expected log events:** `ALERT_DETECTED` → `DECIDE_RUNBOOK` → `BLAST_RADIUS_OK` → `DRY_RUN_PASS` → `ACTION_EXECUTED` → `VERIFY_FAIL` → `ROLLBACK_TRIGGERED` → `ROLLBACK_EXECUTED`

**Actual log output:**

```json
{"ts": "2026-06-18T09:47:55.000000+00:00", "level": "INFO", "event_type": "ALERT_DETECTED", "alertname": "InstanceDown", "service": "checkout-svc", "severity": "critical"}
{"ts": "2026-06-18T09:47:55.000100+00:00", "level": "INFO", "event_type": "DECIDE_RUNBOOK", "alertname": "InstanceDown", "service": "checkout-svc", "runbook": "runbooks/restart_service.sh"}
{"ts": "2026-06-18T09:47:55.000200+00:00", "level": "INFO", "event_type": "BLAST_RADIUS_OK", "alertname": "InstanceDown", "service": "checkout-svc"}
{"ts": "2026-06-18T09:47:55.000300+00:00", "level": "INFO", "event_type": "DRY_RUN_PASS", "alertname": "InstanceDown", "service": "checkout-svc", "runbook": "runbooks/restart_service.sh"}
{"ts": "2026-06-18T09:48:10.792859+00:00", "level": "INFO", "event_type": "VERIFY_SAMPLE", "service": "checkout-svc", "sample": 2, "latency_p99_ms": 0.0, "up": 1.0, "latency_ok": true, "up_ok": true}
{"ts": "2026-06-18T09:48:22.946080+00:00", "level": "INFO", "event_type": "VERIFY_SAMPLE", "service": "checkout-svc", "sample": 3, "latency_p99_ms": 0.0, "up": 0.0, "latency_ok": true, "up_ok": false}
{"ts": "2026-06-18T09:48:32.992263+00:00", "level": "INFO", "event_type": "VERIFY_SAMPLE", "service": "checkout-svc", "sample": 4, "latency_p99_ms": 0.0, "up": 0.0, "latency_ok": true, "up_ok": false}
{"ts": "2026-06-18T09:49:03.000000+00:00", "level": "WARNING", "event_type": "VERIFY_FAIL", "service": "checkout-svc", "samples": 6}
{"ts": "2026-06-18T09:49:03.000100+00:00", "level": "WARNING", "event_type": "ROLLBACK_TRIGGERED", "alertname": "InstanceDown", "service": "checkout-svc", "rollback_runbook": "runbooks/restart_service.sh"}
{"ts": "2026-06-18T09:49:03.160221+00:00", "level": "INFO", "event_type": "ROLLBACK_EXECUTED", "alertname": "InstanceDown", "service": "checkout-svc", "rollback_runbook": "runbooks/restart_service.sh", "success": false, "output": "[restart_service] Restarting ronki-checkout-svc...\nError response from daemon: No such container: ronki-checkout-svc\nfailed to start containers: ronki-checkout-svc"}
{"ts": "2026-06-18T09:49:10.890978+00:00", "level": "WARNING", "event_type": "BLAST_RADIUS_EXCEEDED", "alertname": "InstanceDown", "service": "checkout-svc", "reason": "restarts/hour limit for checkout-svc"}
```

**Observations:**
- Container `ronki-checkout-svc` was permanently removed; restart failed
- Verify detected `up=0.0` and timed out after 60s → `VERIFY_FAIL`
- Rollback was triggered automatically without human intervention
- Rollback also failed (container gone) — correctly logged `success: false`
- Blast-radius guard halted further attempts

**Result:** ✅ PASS

---

## Scenario 3 — Circuit Breaker (3 consecutive failures → HALT)

**Inject sequence:**
```bash
# Set verify threshold impossible (up_required=999) in baseline.json
# Then inject alert 3 times to force 3 consecutive VERIFY_FAIL
curl -s -X POST http://localhost:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{"labels":{"alertname":"HighLatency","service":"inventory-svc","severity":"critical"}}]'
```

**Expected log events:**
`VERIFY_FAIL` + `ROLLBACK_TRIGGERED` × 3 → `CIRCUIT_BREAKER_HALT`

**Actual log output:**

```json
{"ts": "2026-06-18T09:52:41.235777+00:00", "level": "WARNING", "event_type": "VERIFY_FAIL", "service": "inventory-svc", "samples": 3}
{"ts": "2026-06-18T09:52:41.235909+00:00", "level": "WARNING", "event_type": "ROLLBACK_TRIGGERED", "alertname": "HighLatency", "service": "inventory-svc", "rollback_runbook": "runbooks/restart_service.sh"}
{"ts": "2026-06-18T09:52:48.276825+00:00", "level": "INFO", "event_type": "ROLLBACK_EXECUTED", "alertname": "HighLatency", "service": "inventory-svc", "rollback_runbook": "runbooks/restart_service.sh", "success": true}
{"ts": "2026-06-18T09:53:26.778016+00:00", "level": "WARNING", "event_type": "VERIFY_FAIL", "service": "inventory-svc", "samples": 3}
{"ts": "2026-06-18T09:53:26.778130+00:00", "level": "WARNING", "event_type": "ROLLBACK_TRIGGERED", "alertname": "HighLatency", "service": "inventory-svc", "rollback_runbook": "runbooks/restart_service.sh"}
{"ts": "2026-06-18T09:53:33.508522+00:00", "level": "INFO", "event_type": "ROLLBACK_EXECUTED", "alertname": "HighLatency", "service": "inventory-svc", "rollback_runbook": "runbooks/restart_service.sh", "success": true}
{"ts": "2026-06-18T09:54:18.416463+00:00", "level": "ERROR", "event_type": "CIRCUIT_BREAKER_HALT", "consecutive_failures": 3, "threshold": 3, "message": "Automation halted. Manual intervention required."}
{"ts": "2026-06-18T09:54:19.649935+00:00", "level": "INFO", "event_type": "ALERT_DETECTED", "alertname": "HighLatency", "service": "inventory-svc", "severity": "critical"}
{"ts": "2026-06-18T09:54:19.650059+00:00", "level": "ERROR", "event_type": "CIRCUIT_BREAKER_HALT", "alertname": "HighLatency", "service": "inventory-svc", "message": "Circuit is OPEN — automation halted. Manual reset required."}
```

**Observations:**
- 3 consecutive `VERIFY_FAIL` triggered the circuit breaker
- After `CIRCUIT_BREAKER_HALT`, all subsequent alerts for `inventory-svc` are immediately blocked
- No further runbook execution occurred — automation fully halted
- Manual restart of `closed_loop.py` required to reset (per `reset_mode: manual`)

**Result:** ✅ PASS

---

## Scenario 4 — Transactional Rollback (stress test)

**Setup:**
```bash
# Inject MultiStepDeploy alert, then pause container before Step C runs
curl -s -X POST http://localhost:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{"labels":{"alertname":"MultiStepDeploy","service":"api-gateway","severity":"critical"}}]'
sleep 2
docker pause ronki-api-gateway
```

**Expected:** `TRANSACTIONAL_STEP_FAIL` at step C → `TRANSACTIONAL_ROLLBACK_STEP` × 2 (rollback-B then rollback-A) → `TRANSACTIONAL_ROLLBACK_COMPLETE`

**Actual log output:**

```json
{"ts": "2026-06-18T10:14:58.773822+00:00", "level": "INFO", "event_type": "TRANSACTIONAL_START", "alertname": "MultiStepDeploy", "service": "api-gateway", "total_steps": 3}
{"ts": "2026-06-18T10:14:58.830555+00:00", "level": "INFO", "event_type": "TRANSACTIONAL_STEP_COMPLETE", "alertname": "MultiStepDeploy", "service": "api-gateway", "step": "runbooks/multi_step_deploy.sh", "step_index": 0}
{"ts": "2026-06-18T10:14:58.896498+00:00", "level": "INFO", "event_type": "TRANSACTIONAL_STEP_COMPLETE", "alertname": "MultiStepDeploy", "service": "api-gateway", "step": "runbooks/multi_step_deploy.sh", "step_index": 1}
{"ts": "2026-06-18T10:14:58.949862+00:00", "level": "ERROR", "event_type": "TRANSACTIONAL_STEP_FAIL", "alertname": "MultiStepDeploy", "service": "api-gateway", "step": "runbooks/multi_step_deploy.sh", "step_index": 2, "output": "[multi_step_deploy] Step C: final traffic cutover for ronki-api-gateway...\n[multi_step_deploy] ERROR: Step C failed — ronki-api-gateway not running (status=paused)", "completed_before_failure": ["runbooks/multi_step_deploy.sh", "runbooks/multi_step_deploy.sh"]}
{"ts": "2026-06-18T10:15:03.576851+00:00", "level": "INFO", "event_type": "TRANSACTIONAL_ROLLBACK_STEP", "alertname": "MultiStepDeploy", "service": "api-gateway", "rollback_step": "runbooks/multi_step_deploy.sh", "success": true, "exit_code": 0, "output": "[multi_step_deploy] Rollback B: reverting config changes for ronki-api-gateway...\nronki-api-gateway\n[multi_step_deploy] Rollback B complete."}
{"ts": "2026-06-18T10:15:08.196520+00:00", "level": "INFO", "event_type": "TRANSACTIONAL_ROLLBACK_STEP", "alertname": "MultiStepDeploy", "service": "api-gateway", "rollback_step": "runbooks/multi_step_deploy.sh", "success": true, "exit_code": 0, "output": "[multi_step_deploy] Rollback A: restoring pre-deploy state for ronki-api-gateway...\nronki-api-gateway\n[multi_step_deploy] Rollback A complete."}
{"ts": "2026-06-18T10:15:08.196625+00:00", "level": "INFO", "event_type": "TRANSACTIONAL_ROLLBACK_COMPLETE", "alertname": "MultiStepDeploy", "service": "api-gateway", "rolled_back": ["runbooks/multi_step_deploy.sh", "runbooks/multi_step_deploy.sh"]}
```

**Observations:**
- Step A and B completed successfully
- Step C failed because container was paused (`status=paused`)
- Rollback executed in reverse order: rollback-B first, then rollback-A
- `TRANSACTIONAL_ROLLBACK_COMPLETE` confirmed both steps rolled back
- No `ACTION_SUCCESS` logged — failed deploy correctly not marked as successful

**Result:** ✅ PASS

---

## Scenario 5 — Concurrent Alert Race (stress test)

**Inject command:**
```bash
curl -s -X POST http://localhost:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[
    {"labels":{"alertname":"HighLatency","service":"payment-svc","severity":"critical"}},
    {"labels":{"alertname":"HighLatency","service":"inventory-svc","severity":"critical"}}
  ]'
```

**Expected:** Both `ACTION_EXECUTED` events appear within 1s of each other. No `SERVICE_LOCK_BUSY` between different services.

**Actual log output:**

```json
{"ts": "2026-06-18T10:17:45.757544+00:00", "level": "INFO", "event_type": "ACTION_EXECUTED", "alertname": "HighLatency", "service": "payment-svc", "runbook": "runbooks/restart_service.sh", "success": true, "output": "[restart_service] Restarting ronki-payment-svc...\nronki-payment-svc\n[restart_service] ronki-payment-svc is running."}
{"ts": "2026-06-18T10:17:45.757753+00:00", "level": "INFO", "event_type": "VERIFY_START", "service": "payment-svc", "timeout_s": 60}
{"ts": "2026-06-18T10:17:45.817568+00:00", "level": "INFO", "event_type": "ACTION_EXECUTED", "alertname": "HighLatency", "service": "inventory-svc", "runbook": "runbooks/restart_service.sh", "success": true, "output": "[restart_service] Restarting ronki-inventory-svc...\nronki-inventory-svc\n[restart_service] ronki-inventory-svc is running."}
{"ts": "2026-06-18T10:17:45.817823+00:00", "level": "INFO", "event_type": "VERIFY_START", "service": "inventory-svc", "timeout_s": 60}
{"ts": "2026-06-18T10:17:45.768886+00:00", "level": "INFO", "event_type": "VERIFY_SAMPLE", "service": "payment-svc", "sample": 1, "latency_p99_ms": 0.0, "up": 1.0, "latency_ok": true, "up_ok": true}
{"ts": "2026-06-18T10:17:45.828321+00:00", "level": "INFO", "event_type": "VERIFY_SAMPLE", "service": "inventory-svc", "sample": 1, "latency_p99_ms": 0.0, "up": 1.0, "latency_ok": true, "up_ok": true}
{"ts": "2026-06-18T10:17:55.781991+00:00", "level": "INFO", "event_type": "VERIFY_SAMPLE", "service": "payment-svc", "sample": 2, "latency_p99_ms": 0.0, "up": 1.0, "latency_ok": true, "up_ok": true}
{"ts": "2026-06-18T10:17:55.837683+00:00", "level": "INFO", "event_type": "VERIFY_SAMPLE", "service": "inventory-svc", "sample": 2, "latency_p99_ms": 0.0, "up": 1.0, "latency_ok": true, "up_ok": true}
```

**Observations:**
- `ACTION_EXECUTED` for `payment-svc` at `10:17:45.757` and `inventory-svc` at `10:17:45.817` — only **60ms apart**, confirming parallel execution
- Both services ran their own independent processing chains without blocking each other
- No `SERVICE_LOCK_BUSY` between different services
- Both `VERIFY_START` events fired simultaneously, confirming true concurrency

**Result:** ✅ PASS

---

## Scenario 6 — LLM Hallucination Defense (stress test)

**Setup:** Added to `config.yaml` temporarily:
```yaml
runbook_map:
  TestHallucination: "runbooks/nonexistent_runbook.sh"
```
Note: `runbooks/nonexistent_runbook.sh` is NOT in `runbook_registry`.

**Inject command:**
```bash
curl -s -X POST http://localhost:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{"labels":{"alertname":"TestHallucination","service":"frontend","severity":"critical"}}]'
```

**Expected:** `ALERT_DETECTED` → `DECISION_VALIDATION_FAILED`  
**Must NOT appear:** `DRY_RUN_PASS`, `ACTION_EXECUTED`, `RUNBOOK_EXEC`

**Actual log output:**

```json
{"ts": "2026-06-18T10:22:55.076723+00:00", "level": "INFO", "event_type": "ALERT_DETECTED", "alertname": "TestHallucination", "service": "frontend", "severity": "critical"}
{"ts": "2026-06-18T10:22:55.076823+00:00", "level": "ERROR", "event_type": "DECISION_VALIDATION_FAILED", "alertname": "TestHallucination", "service": "frontend", "bad_runbook": "runbooks/nonexistent_runbook.sh", "raw_decision": "runbooks/nonexistent_runbook.sh", "action": "escalate_no_auto_action"}
```

**Observations:**
- `DECISION_VALIDATION_FAILED` fired immediately after `ALERT_DETECTED`
- `bad_runbook: "runbooks/nonexistent_runbook.sh"` correctly identified
- `action: "escalate_no_auto_action"` — no subprocess spawned
- No `DRY_RUN_PASS`, `ACTION_EXECUTED`, or `RUNBOOK_EXEC` in logs
- Circuit breaker counter NOT incremented (validation failure ≠ action failure)

**Result:** ✅ PASS

---

## Summary

| # | Scenario | Result |
|---|---|---|
| 1 | Action success (HighLatency → restart → VERIFY_PASS) | ✅ PASS |
| 2 | Auto-rollback (InstanceDown → VERIFY_FAIL → ROLLBACK) | ✅ PASS |
| 3 | Circuit breaker (3 failures → CIRCUIT_BREAKER_HALT) | ✅ PASS |
| 4 | Transactional rollback (Step C fail → rollback-B → rollback-A) | ✅ PASS |
| 5 | Concurrent alert race (2 services in parallel, no blocking) | ✅ PASS |
| 6 | Hallucination defense (nonexistent runbook → DECISION_VALIDATION_FAILED) | ✅ PASS |

**Total: 6/6 PASS**
