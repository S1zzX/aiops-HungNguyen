# Postmortem: Cloudflare WAF Regex — Catastrophic Backtracking Reproduction (2026-06-17)

> Blameless wording — no "<person name> did X". See §2.1.

## Summary
A globally-deployed WAF rule containing a regex with nested greedy quantifiers
caused catastrophic backtracking on adversarial query strings. Requests matching
the pattern pinned a CPU core for multiple seconds per request, and because the
rule was pushed to all edge nodes simultaneously (no canary), every node was
affected at once. The reproduction confirms the original Cloudflare 2019-07-02
incident's failure mode: a 24-character adversarial input raised request latency
from a 76ms baseline to over 4 seconds — a 54x increase — with no canary buffer
to limit blast radius.

## Impact
- **Users affected:** 100% of traffic hitting the `/` route (reproduction); original
  incident affected ~82% of global edge traffic.
- **Services affected:** `api-gateway` (direct), `frontend` (downstream, cascade
  latency from waiting on `api-gateway`).
- **Revenue/SLA impact:** not modeled in reproduction; see `cost_model.py` for the
  break-even framework that would apply to a real incident of this shape.
- **Duration:** 2026-06-17 07:54:28 UTC (rule deployed) → 07:54:45 UTC (rollback
  confirmed recovered) in the reproduction. Original incident: 2019-07-02
  13:42–14:09 UTC, 27 minutes.

## Timeline (UTC)
Pulled from `timeline.json` (15 events captured; reproduction run, not the original
incident).

| UTC | Event |
|-----|-------|
| 07:54:28 | Baseline measurement starts — WAF rule not yet deployed |
| 07:54:28 | `GET /healthz` baseline latency = 76.3ms, status 200 |
| 07:54:28 | Deploy triggered: new WAF rule pushed globally, no canary (`EVIL_REGEX_ACTIVE=1`) |
| 07:54:29 | Container `api` recreated with rule active |
| 07:54:32 | First user-visible symptom window begins |
| 07:54:32 | `GET /?q=<24x>` latency = 4134.5ms, status 200 (request succeeds but is pinned) |
| 07:54:41 | Repeat probe confirms sustained degradation: latency = 4150.3ms, status 200 |
| 07:54:41 | Synthetic monitor flags p99 breach (4134ms observed vs. 500ms SLO threshold) |
| 07:54:41 | Alert ingested into pipeline: `api-gateway`, `cpu_saturation`, severity critical |
| 07:54:41 | Alert ingested into pipeline: `frontend`, `cascade_latency`, severity warning |
| 07:54:41 | Pipeline queried: `/alerts` returns 2 alerts for the incident window |
| 07:54:41 | Pipeline queried: `/rca` returns `root_service=api-gateway`, confidence 0.7 |
| 07:54:42 | Mitigation applied: WAF rule rolled back globally (`EVIL_REGEX_ACTIVE=0`) |
| 07:54:45 | `GET /healthz` post-rollback latency = 32.1ms, status 200 — recovery confirmed |

## Root cause
The WAF middleware evaluated every request's query string against a regular
expression containing nested unbounded quantifiers (`(?:.*)+` nested inside
another repetition). On inputs that do not contain the literal character the
pattern ultimately requires, the regex engine must exhaust an exponential number
of ways to partition the input before failing — a property of the pattern, not
of any single request. Because the rule was deployed atomically to 100% of
nodes, every node hit this property at the same time, with no smaller-blast-radius
rollout to absorb or limit the failure.

## Contributing factors
1. Global, atomic deployment of WAF rules with no staged rollout (1% → 10% →
   100%), so a single bad pattern affects all traffic immediately.
2. No pre-deploy ReDoS (regular-expression-denial-of-service) testing step in
   the rule-publishing pipeline, so an exponential-time pattern reached
   production undetected.
3. The regex's time complexity is data-dependent — it appears fast on benign
   inputs (e.g., inputs containing `=`, which let the engine match early) and
   only becomes catastrophic on adversarial inputs lacking the terminating
   character, so spot-checking with "normal" traffic during testing would not
   have caught it.

## Detection
- **How was it detected?** Synthetic latency probe (external to the pipeline)
  hitting the service and reporting an SLO breach via `/ingest`. The AIOps
  pipeline itself has no native HTTP-latency probing capability — it depends
  entirely on alerts being pushed to it.
- **MTTD:** In the reproduction, ~13 seconds from rule deployment (07:54:29) to
  alert ingestion (07:54:41), bounded almost entirely by the deliberate pause
  before the probe ran, not by the pipeline's own reaction time.
- **Pipeline gaps observed during reproduction:**
  - Gap 1: The pipeline has no built-in synthetic monitoring or APM
    instrumentation — it is a pure ingest-and-analyze service. Detection
    depended on an external prober explicitly checking latency and calling
    `/ingest`. If no external prober exists for a given route, this failure
    mode produces zero alerts no matter how long it runs.
  - Gap 2: The RCA correctly walked the topology from `frontend` up to
    `api-gateway` (confidence 0.7), but `api-gateway` is the service running
    the WAF middleware, not a separate "WAF" or "edge-rule" node in the
    topology graph. The pipeline cannot distinguish "the service is slow
    because of a bad regex in its middleware" from "the service is slow
    because of generic CPU saturation" — both surface as the same
    `cpu_saturation` fault class with no deeper attribution to the
    responsible code path or recently-deployed rule.

## Response
- **First responder action (reproduction):** rollback executed by reverting
  the WAF rule flag (`EVIL_REGEX_ACTIVE=0`) and recreating the service.
- **Time to mitigate:** ~1 second from decision to rollback command issued
  (07:54:41 → 07:54:42) in the reproduction.
- **Time to fully resolve:** ~4 seconds from rollback command to confirmed
  recovery (07:54:42 → 07:54:45), measured via `/healthz` returning to
  baseline latency (32.1ms).

## Action items
| # | Action | Owner | Type | ETA |
|---|--------|-------|------|-----|
| 1 | Add a pre-deploy ReDoS scanner (e.g., static analysis on every new regex rule) to the WAF rule publishing pipeline | platform-eng | preventive | 2026-07-01 |
| 2 | Require staged rollout (1% → 10% → 100%) for all WAF/edge rule deploys instead of atomic global push | platform-eng | preventive | 2026-07-15 |
| 3 | Add a native synthetic-latency probe to the AIOps pipeline so detection does not depend on an external prober being wired up correctly | aiops-team | detective | 2026-07-08 |
| 4 | Extend the topology graph to model "rule/middleware version" as an attribute of each service node, so RCA can correlate a latency spike with a recent rule change, not just a generic fault class | aiops-team | detective | 2026-07-22 |
