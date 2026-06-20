# Postmortem: Cloudflare WAF Regex — Catastrophic Backtracking Reproduction (2026-06-20)

> Blameless wording — no "<person name> did X". See §2.1.

## Summary
A globally-deployed WAF rule containing a regex with nested greedy quantifiers
caused catastrophic backtracking on adversarial query strings. Requests matching
the pattern pinned a CPU core for multiple seconds per request, and because the
rule was pushed to all edge nodes simultaneously (no canary), every node was
affected at once. The reproduction confirms the original Cloudflare 2019-07-02
incident's failure mode: a 26-character adversarial input raised request latency
from a 280ms baseline to ~9,834ms — a 35× increase — with no canary buffer
to limit blast radius.

## Impact
- **Users affected:** 100% of traffic hitting the `/` route (reproduction); original
  incident affected ~82% of global edge traffic.
- **Services affected:** `api-gateway` (direct), `frontend` (downstream, cascade
  latency from waiting on `api-gateway`).
- **Revenue/SLA impact:** not modeled in reproduction; see `cost_model.py` for the
  break-even framework that would apply to a real incident of this shape.
- **Duration:** 2026-06-20 10:50:46 UTC (rule deployed) → 10:51:31 UTC (rollback
  confirmed recovered) in the reproduction (~45 seconds). Original incident: 2019-07-02
  13:42–14:09 UTC, 27 minutes.

## Timeline (UTC)
Pulled from `timeline.json` (15 events captured; reproduction run 2026-06-20, not the
original incident).

| UTC | Event |
|-----|-------|
| 10:50:45 | Baseline measurement starts — WAF rule not yet deployed |
| 10:50:46 | `GET /healthz` baseline latency = 279.9ms, status 200 |
| 10:50:46 | Deploy triggered: new WAF rule pushed globally, no canary (`EVIL_REGEX_ACTIVE=1`) |
| 10:50:48 | Container `api` recreated with rule active |
| 10:50:57 | First user-visible symptom window begins |
| 10:50:57 | `GET /?q=<26x>` latency = 9,834ms, status 200 (request succeeds but CPU is pinned) |
| 10:51:17 | Repeat probe confirms sustained degradation: latency = 10,039ms, status 200 |
| 10:51:17 | Synthetic monitor flags p99 breach (9,834ms observed vs. 500ms SLO threshold) |
| 10:51:17 | Alert ingested into pipeline: `api-gateway`, `cpu_saturation`, severity critical |
| 10:51:18 | Alert ingested into pipeline: `frontend`, `cascade_latency`, severity warning |
| 10:51:18 | Pipeline queried: `/alerts` returns 2 alerts for the incident window |
| 10:51:22 | Pipeline queried: `/rca` returns `root_service=api-gateway`, confidence 0.7 |
| 10:51:24 | Mitigation applied: WAF rule rolled back globally (`EVIL_REGEX_ACTIVE=0`) |
| 10:51:31 | `GET /healthz` post-rollback latency = 305.8ms, status 200 — recovery confirmed |

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
- **MTTD:** In the reproduction, ~31 seconds from rule deployment (10:50:46) to
  alert ingestion (10:51:17), dominated by the time the adversarial request took
  to complete (~9.8s per request × 2 probes) rather than pipeline reaction time.
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
**What went well:**
- Mitigation was fast once the cause was understood: a single environment
  variable flip (`EVIL_REGEX_ACTIVE=0`) + container recreate fully restored
  service within ~4 seconds of rollback command.
- The topology-aware RCA correctly identified `api-gateway` as the root
  (not the downstream `frontend`), which pointed the responder at the right
  service immediately.
- The service itself did not crash — it remained alive and returned 200s
  throughout — which meant rollback was a config change, not a full redeploy.

**What went poorly:**
- No native detection existed inside the pipeline: detection only happened
  because an external prober was already running and wired to `/ingest`. A
  deployment in a real environment without a pre-existing prober for every
  route would have produced zero alerts, regardless of how long the
  regression ran.
- The rule-deploy pipeline had no staged rollout: a single command affected
  100% of nodes simultaneously, turning a fixable bug into a global outage.
- The RCA output was correct at the service level but could not identify
  the specific cause (a regex deployed inside the middleware); a responder
  would still need to manually inspect recent changes to `api-gateway`.

**Where we got lucky:**
- The adversarial input length that triggers catastrophic backtracking is
  data-dependent: inputs with a trailing `=` character match early and take
  < 3ms. In this reproduction the synthetic prober happened to use an
  input without `=`, which made the failure immediately reproducible. In a
  real deployment, a WAF team might test with "normal" traffic that never
  hits the exponential branch — and deploy with false confidence.
- Recovery time in the reproduction was measured in seconds because Docker
  restarts a container almost instantly. In a production environment with
  a managed edge network (CDN, WAF appliance), a global rule rollback can
  take minutes to propagate to every PoP.

## Action items
| # | Action | Owner | Type | ETA |
|---|--------|-------|------|-----|
| 1 | Add a pre-deploy ReDoS scanner (e.g., static analysis on every new regex rule) to the WAF rule publishing pipeline | platform-eng | preventive | 2026-07-01 |
| 2 | Require staged rollout (1% → 10% → 100%) for all WAF/edge rule deploys instead of atomic global push | platform-eng | preventive | 2026-07-15 |
| 3 | Add a native synthetic-latency probe to the AIOps pipeline so detection does not depend on an external prober being wired up correctly | aiops-team | detective | 2026-07-08 |
| 4 | Extend the topology graph to model "rule/middleware version" as an attribute of each service node, so RCA can correlate a latency spike with a recent rule change, not just a generic fault class | aiops-team | detective | 2026-07-22 |
