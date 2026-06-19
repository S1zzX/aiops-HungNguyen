# DESIGN.md — Closed-Loop Orchestrator Design Defense

**Author:** HungNguyen  
**Lab:** AIOps — Closed-Loop Auto-Remediation  
**Stack:** Ronki e-commerce platform (5 FastAPI services + Prometheus + Alertmanager)

---

## Question 1 — Decision Engine: Rule-based or LLM-based?

**Choice: Rule-based (Option A)**

### Implementation

```python
RUNBOOK_MAP = {
    "HighLatency":   "runbooks/restart_service.sh",
    "HighErrorRate": "runbooks/clear_cache.sh",
    "InstanceDown":  "runbooks/restart_service.sh",
}
```

Defined in `config.yaml` under `runbook_map` and loaded at startup — no hardcoding.

### Rationale

| Dimension | Rule-based ✅ | LLM-based |
|---|---|---|
| **Determinism** | Same input → always same action | Non-deterministic; same alert may produce different actions |
| **Latency** | < 1 ms decision time | 500–2000 ms API round-trip |
| **Audit trail** | Exact mapping logged (`DECIDE_RUNBOOK`) | Confidence score + raw response must be captured |
| **Reliability** | No external dependency | Fails if Anthropic API is unreachable |
| **Blast-radius risk** | Explicit, bounded | Hallucinated runbooks are a real risk |

### Trade-offs

- **Rule-based weakness:** Cannot handle novel alert types not in the map (`NO_RUNBOOK_MAPPED` is logged and the alert is escalated). For a production AIOps system at scale, LLM-based would improve coverage.
- **LLM-based strength:** Can reason over multi-signal context (latency + error rate + pod restart count simultaneously) to choose the best runbook rather than the first matching rule.
- **Mitigation in this implementation:** The `runbook_registry` provides a hallucination defense layer — even if a bad decision is made (manually or by an LLM), only pre-approved scripts may execute (Scenario 6).

---

## Question 2 — Blast-Radius Configuration

**Values** (in `config.yaml`):

```yaml
blast_radius:
  max_actions_per_minute: 3
  max_restarts_per_service_per_hour: 5
```

### Rationale

**`max_actions_per_minute: 3`**  
Ronki processes ~80,000 orders/day ≈ 55 orders/minute at peak. A burst of automated actions has a fan-out effect: restarting `api-gateway` affects all 5 downstream services. Limiting to 3 actions/minute gives each action ~20 seconds to propagate before the next fires, reducing the risk of a remediation cascade amplifying the incident.

**`max_restarts_per_service_per_hour: 5`**  
If the same service needs more than 5 restarts per hour, the root cause is NOT transient (e.g., a bad deploy, OOM loop, or upstream dependency failure). At that point, automation is likely making things worse. 5 is derived from the Kubernetes pod restart back-off policy (CrashLoopBackOff kicks in after ~5 rapid restarts), aligning automation with the expected container lifecycle.

**Behavior on exceed:** The orchestrator logs `BLAST_RADIUS_EXCEEDED` and escalates (no action taken). The alert remains in Alertmanager and will be re-evaluated in the next poll cycle.

---

## Question 3 — Verify Step: Metric, Threshold, Timeout

**Metrics checked** (from `engine/verify.py`, thresholds from `data/baseline.json`):

| Metric | PromQL | Threshold | Pass Condition |
|---|---|---|---|
| `latency_p99_ms` | `histogram_quantile(0.99, rate(http_request_duration_seconds_bucket{service="X"}[1m])) * 1000` | < **500 ms** | latency drops below threshold |
| `up` | `up{job="X"}` | **== 1** | service is reachable |
| `error_rate_pct` | `rate(http_requests_total{service="X",status="500"}[2m]) / (rate(...) + 0.001) * 100` | < **10 %** | error rate below threshold |

**Timing parameters:**

```json
"verify_timeout_seconds":       60,
"verify_poll_interval_seconds": 10,
"verify_min_samples":            3
```

**Pass logic:** ALL three metrics must be within threshold for **3 consecutive samples** within a **60-second window**. Requiring 3 consecutive passes (not just 3 total) prevents a flapping service from triggering a false positive.

**Fail path:** If the deadline expires without 3 consecutive passes → `VERIFY_FAIL` is logged → `ROLLBACK_TRIGGERED` is fired automatically (no human action required).

**Note on error_rate query:** `baseline.json` originally referenced `http_errors_total`, a metric the mock service does not emit. `verify.py` uses the correct metric `http_requests_total{status="500"}` instead.

---

## Question 4 — Circuit Breaker: Reset Policy

**Config:**

```yaml
circuit_breaker:
  consecutive_failure_threshold: 3
  reset_mode: manual
```

**Trigger:** After **3 consecutive verify failures** on the same service, `CircuitBreaker.record_failure()` sets `_open = True` and logs:

```json
{
  "event_type": "CIRCUIT_BREAKER_HALT",
  "consecutive_failures": 3,
  "message": "Automation halted. Manual intervention required."
}
```

**Reset: Manual (operator must restart `closed_loop.py`)**

### Rationale

An automatic reset (e.g., after 30 minutes) would allow automation to resume even if the root cause has not been resolved. For an e-commerce platform handling ~1,000 orders per 15-minute window, resuming automation without human verification could:

1. Execute the same failing runbook again, wasting remediation budget
2. Mask a deeper incident (e.g., bad deploy or infrastructure failure) behind repeated automated restarts
3. Exhaust the blast-radius budget on the already-failing service

**Manual reset forces a human to:**
- Check the Grafana dashboard to confirm the service is actually healthy
- Review the audit log (`audit_log.jsonl`) to understand the failure chain
- Restart the orchestrator once satisfied

This aligns with the principle of "keep the human in the loop after repeated failures" — automation handles the first attempt, humans handle the escalation.

---

## Summary

The orchestrator implements all 5 safety sub-checkpoints:

| # | Sub-checkpoint | Implementation |
|---|---|---|
| 1 | Dry-run mode | Every runbook always called with `--dry-run` first; orchestrator also supports `--dry-run` flag |
| 2 | Blast-radius | `BlastRadiusGuard(max_per_minute=3, max_restarts_per_hour=5)` from `engine/safety.py` |
| 3 | Verify post-act | `verify_service()` polls Prometheus ≥3 times within 60s; requires 3 consecutive passes |
| 4 | Auto-rollback | `_rollback()` fires automatically on `VERIFY_FAIL` — no human trigger needed |
| 5 | Circuit breaker | `CircuitBreaker(threshold=3)` per service; opens after 3 consecutive failures; manual reset |

Additional safety features beyond the 5 sub-checkpoints:
- **Per-service mutex** — prevents two runbooks executing concurrently on the same service
- **Runbook registry** — rejects any runbook path not in the pre-approved list (hallucination defense)
- **Dedup guard** — prevents the same (alert, service) pair from spawning duplicate threads
- **Structured audit log** — every event has `ts`, `event_type`, `service`, and contextual fields
