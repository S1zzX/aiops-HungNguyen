# ADR-001: Add native synthetic-latency probing instead of relying solely on externally-pushed alerts

> Format: Nygard (2011)

## Status
Accepted

## Context
The Cloudflare-regex reproduction (see `postmortem.md`, Detection §Gap 1) showed
that the current pipeline has no mechanism of its own for noticing that a
service has become slow. It only reacts to alerts that something else pushes
into it via `/ingest`, or to whatever Prometheus happens to expose through
`_pull_prometheus_alerts()` — and that call silently swallows any connection
failure and returns an empty list. In the reproduction, detection only happened
because we manually wrote and ran an external prober script that hit `/healthz`
and `/?q=...` and called `/ingest` ourselves. In a real deployment, if no one
remembers to wire up that prober for a given route, a catastrophic-backtracking
regex (or any other silent-CPU-pin failure mode) would produce zero alerts no
matter how long it ran, because nothing is actively watching for it.

The pipeline needs some way to detect "this previously-fast endpoint is now
slow" without requiring every team to remember to build and maintain their own
external prober for every route they care about.

## Decision
Add a built-in synthetic-latency prober to the pipeline that periodically polls
a configurable list of endpoints, compares each measurement against a rolling
baseline, and calls the pipeline's own `/ingest` logic internally when an
endpoint's latency exceeds its baseline by a defined multiple — so detection
no longer depends on an external script being written and kept running.

## Alternatives considered
1. **Keep relying on Prometheus scrape + external probers** — simplest, zero
   new code, matches the original design intent (`_pull_prometheus_alerts`).
   Rejected because it only works if Prometheus and a prober are both
   correctly configured and reachable; the reproduction showed the pipeline
   degrades silently (empty list, no error) when that dependency chain breaks,
   which is exactly the situation that hid the regex outage from detection.
2. **Require every service to self-report latency via a sidecar** — pushes the
   responsibility to each service team, which is architecturally cleaner
   (each service owns its own instrumentation). Rejected as the primary fix
   because it does not help with already-deployed services that have no
   sidecar yet, and the gap we observed needs to be closed centrally, not
   per-team, to avoid silent coverage holes.
3. **Add log-based detection (parse access logs for slow requests)** — would
   catch the regex case specifically, since slow requests do show up in
   access logs with high response time. Rejected as the sole mechanism
   because it adds a dependency on log shipping being correctly configured
   and parseable, which has the same "silently absent" failure mode as the
   Prometheus dependency we are trying to move away from.

## Consequences
- **Positive:** detection no longer depends on a human remembering to write
  and run an external prober per route; the pipeline can claim a real,
  bounded MTTD for any endpoint it has been told to watch, rather than an
  MTTD that is actually "however long it takes someone to notice and write a
  check."
- **Negative:** the pipeline now needs to store and maintain rolling latency
  baselines per endpoint, which is new state and new failure surface (a
  noisy or wrong baseline produces false positives/negatives).
- **Risks introduced:** the prober itself becomes a single point of failure
  for detection — if it crashes or its endpoint list goes stale, coverage
  silently degrades again, just one layer further down. Mitigation: alert on
  the prober's own heartbeat, not just on what it reports.
- **What gets locked in:** every team that wants pipeline coverage for a new
  route must register it with the prober's endpoint list; this is a small
  ongoing operational cost but is explicit and visible (a config list),
  unlike the current implicit dependency on someone externally building a
  check.
