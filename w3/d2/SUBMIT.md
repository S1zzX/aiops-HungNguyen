# W3-D2 Submission — Hung Nguyen

## 3 things I learned about my AIOps pipeline

1. **Infrastructure-layer faults are invisible to an app-only detector.** Disk fill on log-collector and DNS latency on dns-resolver both produced zero alerts because the detector only scrapes HTTP-layer metrics. A real pipeline needs node_exporter and blackbox_exporter to cover the infra tier — otherwise entire fault classes are permanently blind spots.

2. **MTTD varies dramatically by fault type — from 12s to 55s.** Availability faults (container kill) are detected almost instantly because health checks fail on the next scrape. Resource-saturation faults (memory, CPU) take 40-55s because the metric builds gradually and only crosses the threshold after several scrape cycles. This means the pipeline has very different SLOs depending on what broke.

3. **Topology-aware RCA is essential — alert count alone is misleading.** The retry-storm experiment (exp 10) showed that the service with the most alerts (checkout-svc) is NOT the root cause. A count-based ranker picks the noisiest service every time, which is the exact wrong answer in a retry-storm scenario. The RCA engine must walk the dependency graph upstream.

---

## 1 fault I expected the pipeline to catch but it missed

**Experiment**: exp 9 — dns_slow_lookup (dns_latency fault on dns-resolver)

**Why I expected detection**: DNS is a shared dependency for every service in the stack. A +2s lookup delay should cascade into intermittent connection errors across multiple services simultaneously, which should produce a detectable multi-service anomaly signal.

**Why the pipeline missed (hypothesis)**: DNS faults are intermittent by nature — services only re-resolve DNS on connection reset or cache expiry, not on every request. The errors appear sporadically across services, never building into a sustained signal that crosses the detector's threshold within a single evaluation window. The detector needs either a DNS-specific probe (blackbox_exporter) or a longer evaluation window to accumulate enough intermittent errors to fire.

---

## 1 trade-off in pipeline design I want to rethink

**Scrape interval vs detection speed for resource faults.**

Currently the pipeline scrapes every 10s and uses static absolute thresholds. This works well for binary faults (container kill = instant gap) but is too slow for gradual resource saturation — memory fill took 55s to detect. The obvious fix is to reduce scrape interval to 5s, but this doubles Prometheus storage and CPU usage.

A better trade-off is **rate-of-change alerting**: instead of alerting when `memory_usage > 95%`, alert when `rate(memory_usage[2m]) > 5% per minute`. This fires earlier (when memory is at 70% but growing fast) without requiring a faster scrape interval, and avoids false alarms on services that legitimately use high memory at steady state.

---

## Scoreboard Summary

| Metric | Value |
|---|---|
| detected | 8/10 |
| rca_correct | 7/8 |
| mttd_p50 | 30s |
| mttd_p95 | 55s |
| false_alarms | 0 |
| precision | 1.00 |
| recall | 0.80 |
| verdict | **PASS** |
