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
- Target SLO: 99.5% successful, sub-500ms requests for `api-gateway` and
  `checkout-svc` (the two services exercised in this week's reproduction).
- SLI: HTTP request latency p99 < 500ms AND status code not in 5xx.
- Error budget: 0.5% of monthly request volume (~3.6 hours of full-degradation
  equivalent per 30-day month).
- Burn-rate alert tiers: fast burn (budget exhausted in <2h → page immediately),
  slow burn (budget exhausted in <7 days → ticket, review next business day).

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
- Chaos run cadence: not yet established for this stack — recommend starting
  at weekly, scoped to non-production, before promoting to a monthly
  production-adjacent cadence.
- Detected/total ratio target: 90% of injected faults should produce at least
  one correctly-attributed alert within the SLO's fast-burn window.
- Steady-state signal: synthetic probe (latency + status code), the same
  mechanism used for detection in §3, doubling as the steady-state hypothesis
  check before/after each chaos experiment.

## 5. Operational pattern (from W3-D3)
- Postmortem template: `postmortem.md` (Google SRE format, blameless wording
  enforced — see §2.1 of the course notes).
- On-call rotation: not yet defined for this exercise; the cost model in §6
  treats on-call hours as a separate line item once a rotation exists.
- ADR repository: `ADR.md` (ADR-001, Nygard format) — this week's decision was
  to add native synthetic-latency probing to close the detection gap found
  during reproduction (see §5 below and `postmortem.md` Detection §Gap 1).

## 6. Cost model (from W3-D3)
- Monthly cost (my scenario): $18,000/month for AIOps platform overhead.
- Break-even avoided incidents/month: with 4 incidents/month at 1.5h average
  duration and $15k/hour downtime cost, the platform returns ROI = 2.0
  (`verdict: worth_it`), with payback in well under a month. See
  `cost_model.py` Scenario 3 for the full input set and reasoning behind each
  number.
- See `cost_model.py` for the `is_worth_it()` implementation and three worked
  scenarios (two from the course's §8.4 table, one original).

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
