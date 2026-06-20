# AIOps Mini-Platform Spec — HungNguyen

## 1. Platform overview
This platform monitors an e-commerce-style stack (`frontend`, `api-gateway`,
`checkout-svc`, `payment-svc`, `inventory-svc`, `auth-svc`, plus supporting
services like `payment-db`, `inventory-db`, `dns-resolver`, `log-collector`).
Scope: detect anomalies, correlate them into incident clusters, and run
topology-aware root-cause analysis. Non-scope (for this iteration): automated
remediation, capacity planning, and cost forecasting beyond the break-even
model in §6.

## 2. SLO definition (from W3-D1)
Source: `w3/d1/slo_spec.yaml` (version 1, 3 services, 30-day rolling window).

| Service    | SLI kind     | SLO target | Monthly error budget (events) | Downtime equiv. |
|------------|--------------|------------|-------------------------------|-----------------|
| `frontend` | availability | 99.0%      | 51,840 bad events / 5,184,000 | ~5 min          |
| `api`      | availability | 99.0%      | 207,378 bad events / 20,737,800 | ~20 min        |
| `db`       | availability | 99.9%      | 1,726 bad events / 1,726,380  | ~43 min         |

**SLI formulas:**
- `frontend`: `count(requests where js_error=false AND network_error=false) / count(all requests)`
- `api`: `count(status NOT IN 5xx, 429) / count(all requests)`
- `db`: `count(success=true) / count(all queries)`

**Burn-rate alert tiers** (derived from W3-D1, see `burn_rate_alerts.yaml`):
- Fast burn: budget exhausted in < 2h → page immediately (SEV1).
- Slow burn: budget exhausted in < 7 days → ticket, review next business day (SEV3).

## 3. Detection + Correlation + RCA stack (from W1+W2)
- **Detector:** synthetic latency probing (external prober in this exercise;
  see ADR-001 for the decision to bring this in-house) comparing live latency
  against a rolling baseline; outputs an `IngestAlert` (`service`,
  `fault_class`, `severity`, `fire_ts`) into the pipeline's `/ingest` endpoint.
- **Correlator:** `/correlate` groups alerts inside a time window by walking
  each alerted service's upstream dependency chain in the `TOPOLOGY` map,
  producing one cluster per unique root candidate with a member list and
  alert count.
- **RCA:** `/rca` (topology-aware) classifies each alerted service as either a
  root candidate (not downstream of any other alerted service) or a symptom
  candidate, then picks the root candidate with the fewest upstream
  dependencies, with confidence scaled by how many alerts that service
  produced. Falls back to highest-alert-count if no clear root candidate
  exists. Output schema: `{root_service, confidence, evidence, reasoning}`.

## 4. Reliability validation (from W3-D2)
Source: `w3/d2/chaos_report.md` (10 experiments, 2026-06-16, stack: w3-d2-pack).

**Scoreboard:**
```
Total: 10  |  Detected: 8/10  |  RCA correct: 7/8
False alarms in baseline windows: 0
Precision: 1.00  |  Recall: 0.80
MTTD p50: 30s  |  MTTD p95: 55s
```

**Per-experiment summary:**

| # | Fault | Detected | MTTD | RCA correct |
|---|-------|----------|------|-------------|
| 1 | payment_latency | Y | 28s | Y |
| 2 | payment_network_loss | Y | 35s | Y |
| 3 | inventory_pod_kill | Y | 12s | Y |
| 4 | apigateway_cpu_stress | Y | 42s | Y |
| 5 | paymentdb_memory_fill | Y | 55s | Y |
| 6 | authsvc_clock_skew | Y | 30s | Y |
| 7 | logcollector_disk_fill | **N** | — | N |
| 8 | gateway_network_partition | Y | 15s | Y |
| 9 | dns_slow_lookup | **N** | — | N |
| 10 | checkout_retry_storm | Y | 20s | **N** |

**Top 3 gaps:**

1. **Infra-layer blind spot (exp 7, 9):** Disk fill on `log-collector` and DNS
   latency on `dns-resolver` both produced zero alerts. The detector only scrapes
   application-layer metrics (HTTP latency, error_rate, container availability)
   and has no node_exporter/blackbox_exporter coverage for infra-tier metrics.
   Fix: add node_exporter scraper for disk I/O + blackbox DNS probe.

2. **Retry-storm RCA failure (exp 10):** Pipeline detected the fault correctly
   but RCA returned `checkout-svc` (the symptom carrier) instead of the upstream
   degraded service. Count-based ranking picks the highest-alert-count service,
   which in a retry storm is always the downstream relay, not the root.
   Fix: topology-aware RCA that deprioritizes downstream-only alert clusters
   (see ADR-001 context and `pipeline/main.py` `_topology_rca()`).

3. **Slow MTTD for resource-saturation faults (exp 4: 42s, exp 5: 55s):**
   CPU and memory pressure build gradually; a fixed scrape interval means
   the detector is 10–50s behind for slow-ramp faults. Fix: predictive
   thresholding on rate-of-change + reduce scrape interval to 5s for critical
   infra targets.

## 5. Operational pattern (from W3-D3)
- Postmortem template: `postmortem.md` (Google SRE format, blameless wording
  enforced — see §2.1 of the course notes).
- On-call rotation: not yet defined for this exercise; the cost model in §6
  treats on-call hours as a separate line item once a rotation exists.
- ADR repository: `ADR.md` (ADR-001, Nygard format) — this week's decision was
  to add native synthetic-latency probing to close the detection gap found
  during reproduction (see §5 below and `postmortem.md` Detection §Gap 1).

## 6. Cost model (from W3-D3)
Source: `w3/d3/d3_sub/cost_model.py` — `is_worth_it()` per W3-D3 §8.3 spec.

**Stack scenario (Scenario 3 — mid-tier e-commerce checkout):**
```
Inputs:
  num_services              = 60
  incidents_per_month       = 4
  avg_incident_duration_h   = 1.5
  downtime_cost_per_hour    = $15,000   (mid-tier e-commerce, §8.2 band)
  expected_mttr_reduction   = 40%
  aiops_monthly_cost        = $18,000

Output:
  monthly_value   = 4 × 1.5 × 0.40 × $15,000 = $36,000
  monthly_cost    = $18,000
  roi             = 2.0
  payback_months  = 0.5
  verdict         = worth_it
```

**Break-even point:** 3 incidents/month × 1.5h at $15k/hour is the minimum
threshold for this platform cost to return ROI ≥ 1.0. Below that (e.g.
2 incidents/month), the platform is marginal and a better-tuned on-call
rotation is the right investment first (see W3-D3 §8.5 — "When NOT to do
AIOps").

**Reference scenarios** (from §8.4 table):
- 20 svc, 2 inc/mo × 1h, $10k/h → ROI = 0.53 → `not_worth_it`
- 100 svc, 5 inc/mo × 2h, $20k/h → ROI = 3.2 → `worth_it`

## 7. Open risks
- **Risk 1 (severity: high):** Detection currently depends entirely on
  externally-pushed alerts; if no prober is wired up for a given route, a
  silent-CPU-pin failure (like the regex case reproduced this week) produces
  zero alerts no matter how long it runs. Mitigation: ADR-001 (native
  synthetic prober), tracked as action item #3 in `postmortem.md`.
- **Risk 2 (severity: medium):** The topology graph models services, not the
  middleware/rule layer running inside them, so RCA cannot distinguish "this
  service is slow because of a bad regex deployed today" from generic CPU
  saturation. Mitigation: action item #4 in `postmortem.md` (model rule/version
  as a node attribute).
- **Risk 3 (severity: medium):** `_pull_prometheus_alerts()` silently returns
  an empty list on any connection failure, which means a broken Prometheus
  connection looks identical to "no anomalies" — there's no distinct signal
  for "the monitoring pipeline itself can't see anything," which is exactly
  the failure shape that hid the Roblox 2021 outage from on-call for ~12
  hours (see course notes §4.4).
- **Risk 4 (severity: low):** No staged-rollout requirement exists at the
  platform-config level for rule changes (WAF or otherwise) feeding into this
  stack; a single bad config can still reach 100% of nodes atomically, as it
  did in the original Cloudflare 2019 incident this week's reproduction is
  based on.
- **Risk 5 (severity: low):** The break-even cost model in §6 does not yet
  account for engineer time spent maintaining the topology graph as services
  are added/removed/renamed, which §11 of the course notes flags as a common
  way cost models underestimate true cost by 3-5x.
