# RCA Report — ronki-shop · March 2026 (30-day window)

## Incident Index

| Incident ID | Window (UTC) | One-line summary |
|---|---|---|
| I-1 | 2026-03-05 14:20 → 15:10 | fx-api 503 storms + disabled retry jitter exhausted payment-svc connection pool |
| I-2 | 2026-03-11 02:45 → 04:30 | inventory-svc numpy 2.0 memory leak OOM-killed container under promo traffic |
| I-3 | 2026-03-17 11:15 → 12:05 | loyalty-recommendations feature flag triggered unindexed RDS full-table scans |
| I-4 | 2026-03-22 09:00 → 09:40 | pp-api IP block rotation left AZ-c with stale DNS, partial payment failures |
| I-5 | 2026-03-27 06:00 → 06:30 | mTLS cert rotation clock skew caused certificate_not_yet_valid handshake failures |

---

## I-1 · 2026-03-05 14:20 → 15:10

### 1. Timeline

| Timestamp (UTC) | Event | Source |
|---|---|---|
| 13:58 | `inventory-svc` v2.4.1 deployed (`warehouse_timeout_ms 5000→3000`) | `deploy_log.json` |
| ~14:20 | `fx-api` begins returning sporadic 503 responses | `metrics/fx_api_5xx_per_min.csv` ~14:20 |
| 14:32 | First error traces: `payment-svc.fx_client.convert` fails with `fx_503_after_3_retries`; each attempt takes 1800–2500 ms; 3 attempts per call | `traces.json` trace `6a870411de257668` |
| 14:32:15 | Alert A-001 `CheckoutP99High` fires — p99 > 1500 ms | `alerts.json` A-001 |
| 14:33:00 | Alert A-002 `CheckoutErrorRateHigh` fires — error rate > 5% | `alerts.json` A-002 |
| 14:34:30 | Alert A-003 `RedisHitRateLow` fires — hit rate < 70% | `alerts.json` A-003 |
| 14:35:00 | Alert A-004 `PaymentConnPoolSaturated` fires — pool > 90% (pool_max=200) | `alerts.json` A-004 |
| 14:35–14:44 | Multiple traces show `conn_pool_acquire_timeout` on `payment-svc.charge` and `fx_503_after_3_retries` simultaneously | `traces.json` traces `1502a3e2`, `a8c2a22d`, `c66e83d1`, `b61c3417`, `4c14e51e`, `c873ad55` |
| 14:36:00 | Alert A-005 `RdsCpuHigh` fires — CPU > 80% | `alerts.json` A-005 |
| 14:36:00 | Alert A-007 `SyntheticCheckoutFailing` fires | `alerts.json` A-007 |
| 14:38:00 | Alert A-006 `PaymentMemoryHigh` fires — heap > 85% | `alerts.json` A-006 |
| 14:39:00 | Alert A-008 `FxApi5xxObserved` fires | `alerts.json` A-008 |
| ~15:10 | Metrics return to baseline; fx-api 5xx drops to 0 | `metrics/fx_api_5xx_per_min.csv` |

### 2. Candidate Hypotheses

| # | Hypothesis | Confidence (1–5) | Initial reasoning |
|---|---|---|---|
| H1 | fx-api returning 503 + retry jitter disabled caused thundering herd on connection pool | 5 | Traces show 3 retries per fx call, each 1.8–2.5s; flag `fx_retry_jitter` confirmed disabled since 2026-02-01 |
| H2 | inventory-svc deploy (warehouse_timeout 5000→3000) caused cascading failures | 3 | Deploy fired 34 min before incident; could have increased timeout errors propagating upstream |
| H3 | Redis cache eviction / hit-rate drop caused checkout slowdown | 2 | A-003 fired at 14:34:30; could be primary cause driving latency up |
| H4 | RDS CPU spike caused database contention propagating to payment-svc | 2 | A-005 fired 3.5 min after first alert; RDS is downstream of payment-svc |

### 3. Evidence Review

> **Hypothesis H1 — fx-api 503 + disabled retry jitter**
> **Verdict**: Accepted
> **Evidence**: All 8 error traces during the incident window contain `payment-svc.fx_client.convert` with `fx_503_after_3_retries` and three explicit retry attempts at 0ms jitter offset (attempt 1: 1800ms, attempt 2: 2200ms, attempt 3: 2500ms — gap is linear, not exponential+jitter). `deploy_log.json` entry at 2026-03-05T12:30:00Z confirms `fx_retry_jitter=disabled` since 2026-02-01. The 3-attempt × ~2s pattern means every in-flight checkout held a connection slot for 6.5+ seconds, exhausting the 200-slot pool.

> **Hypothesis H2 — inventory-svc deploy**
> **Verdict**: Dismissed
> **Evidence**: All error traces during the incident span only `payment-svc.fx_client.convert` and `checkout-svc` — none involve `inventory-svc` or `warehouse-api`. The checkout p99 spike correlates precisely with the fx-api 5xx spike (`metrics/fx_api_5xx_per_min.csv`), not with any inventory-svc metric. The 34-minute gap between the deploy and the incident further weakens the causal link.

> **Hypothesis H3 — Redis hit-rate drop as primary cause**
> **Verdict**: Dismissed
> **Evidence**: Alert A-003 (RedisHitRateLow) fired at 14:34:30, which is 2 minutes and 15 seconds *after* A-001 (CheckoutP99High at 14:32:15). `metrics/redis_hit_rate.csv` shows the drop is a consequence of increased checkout errors (fewer successful requests → fewer cache writes), not a cause. Redis sits downstream of checkout-svc in the call graph.

> **Hypothesis H4 — RDS CPU as primary cause**
> **Verdict**: Dismissed
> **Evidence**: Alert A-005 (RdsCpuHigh) fired at 14:36:00, more than 3 minutes after the first checkout p99 alert. In the topology, `rds-orders` is a dependency of `payment-svc`, not `checkout-svc` directly. Traces show `payment-svc.charge` failing with `conn_pool_acquire_timeout`, not with database errors. The RDS CPU rise is a side-effect of payment-svc holding connections open during retries (keeping DB connections alive longer).

### 4. Root Cause

**Root cause**: `fx-api` (external currency-conversion service) began returning HTTP 503 responses at approximately 14:20 UTC. `payment-svc` is configured to retry fx calls up to 3 times with no jitter (feature flag `fx_retry_jitter=disabled`). Each checkout request that triggered a currency conversion held an outbound HTTP connection for the full retry duration (~6.5 seconds total). With the 200-slot connection pool, concurrent requests rapidly exhausted available connections. Subsequent requests failed immediately with `conn_pool_acquire_timeout`, propagating errors and high latency upstream to `checkout-svc` and `frontend`.

**Causal chain**:
```
fx-api returns 503
  → payment-svc retries 3x with no jitter (each attempt ~2s)
    → each in-flight request holds 1 connection slot for ~6.5s
      → 200-slot connection pool saturates (A-004)
        → new checkout requests: conn_pool_acquire_timeout
          → checkout-svc p99 > 1500ms (A-001), error rate > 5% (A-002)
            → Redis hit rate drops (fewer successful requests) (A-003)
            → RDS CPU rises (held connections keep DB txns open) (A-005)
```

### 5. Counterfactual

- **H2 (inventory deploy)**: Reducing warehouse timeout would only affect `inventory-svc → warehouse-api` calls; the root failure path runs entirely through `payment-svc → fx-api` and would have fired identically.
- **H3 (Redis)**: Restoring Redis to 100% hit rate would not have resolved the `conn_pool_acquire_timeout` errors in `payment-svc`, since those timeouts occur before checkout-svc even reaches the Redis lookup step.
- **H4 (RDS)**: Upgrading the RDS instance (done on 2026-03-16) would not have prevented this incident because no database query error appears in any trace during this window; the RDS CPU elevation was a downstream side-effect.

### 6. Prevention

1. **payment-svc / fx_client**: Add exponential backoff with full-jitter to all `fx-api` retry logic. Measurable criterion: p99 retry-hold time < 500ms under sustained 503 conditions (testable via chaos injection in staging with `fx_api_mock_503_rate=100%`).
2. **payment-svc / connection pool**: Implement per-upstream connection budgets with circuit breaker. When `fx-api` error rate exceeds 50% over a 30-second window, open the circuit and return a fast-fail to callers within 100ms instead of exhausting pool slots. Measurable: `payment_conn_pool_active` must not exceed 70% utilisation during a simulated fx-api outage.

---

## I-2 · 2026-03-11 02:45 → 04:30

### 1. Timeline

| Timestamp (UTC) | Event | Source |
|---|---|---|
| 2026-03-10 02:00 | `inventory-svc` v2.5.0 deployed: `numpy 1.26 → 2.0.0` | `deploy_log.json` |
| 2026-03-11 02:15 | `homepage_promo_carousel` feature flag enabled by marketing-team | `deploy_log.json` |
| 02:45:00 | Alert A-101 `InventoryP99High` fires — p99 > 150ms for 5 min | `alerts.json` A-101 |
| 03:00:00 | Alert A-102 `InventoryMemoryHigh` fires — RSS > 2000 MB and growing | `alerts.json` A-102 |
| 03:00 | Trace `1492caa9`: `inventory-svc.stock_lookup` fails with `gc_pause` error | `traces.json` |
| 03:20 | Trace `db284d4b`: repeated `gc_pause` on `inventory-svc.stock_lookup` | `traces.json` |
| 03:20:00 | Alert A-103 `CheckoutErrorRateHigh` fires — error rate > 3% for 5 min | `alerts.json` A-103 |
| 03:40, 04:00, 04:20, 04:40 | Successive traces all show `gc_pause` on inventory-svc | `traces.json` (6 traces) |
| 04:18:00 | Alert A-104 `InventoryOOMKilled` fires — container OOM-killed | `alerts.json` A-104 |
| ~04:30 | `inventory_oom_kills_per_min` returns to 0; service restarted | `metrics/inventory_oom_kills_per_min.csv` |

### 2. Candidate Hypotheses

| # | Hypothesis | Confidence (1–5) | Initial reasoning |
|---|---|---|---|
| H1 | numpy 2.0 introduced a memory leak causing gradual heap growth → OOM | 5 | Deploy 22h before incident; RSS grew monotonically from 02:45 to OOM at 04:18 per A-102 |
| H2 | homepage_promo_carousel traffic spike overloaded inventory-svc | 3 | Flag enabled 30 min before first alert; increased frontend traffic → more stock lookups |
| H3 | warehouse-api degradation caused slow responses piling up in memory | 2 | inventory-svc depends on warehouse-api; slow external calls can accumulate response buffers |
| H4 | rds-inventory contention caused checkout-svc errors (not inventory OOM) | 1 | Checkout errors could originate from DB rather than inventory memory |

### 3. Evidence Review

> **Hypothesis H1 — numpy 2.0 memory leak**
> **Verdict**: Accepted
> **Evidence**: `metrics/inventory_memory_mb.csv` shows a monotonically increasing RSS trend starting shortly after the v2.5.0 deploy on 03-10 02:00, crossing 2000 MB threshold at 03:00 (A-102). GC pause errors (`gc_pause` in 6 consecutive traces from 03:00 to 04:40) are characteristic of a JVM/Python runtime under extreme heap pressure. The OOM kill at 04:18 (A-104) is the terminal event. numpy 2.0 changed internal buffer management APIs that can cause unbounded allocations when called with legacy array protocols — consistent with a production workload not present in the 10k-user staging test.

> **Hypothesis H2 — promo carousel traffic spike**
> **Verdict**: Partially accepted as contributing factor, not root cause
> **Evidence**: The `homepage_promo_carousel` flag was enabled at 02:15 — 30 minutes before the first alert. `metrics/frontend_req_rate.csv` shows a modest request rate increase after 02:15. The traffic increase alone would not have caused OOM (memory growth started before the flag), but it accelerated heap growth by increasing the frequency of numpy array allocations, shortening time-to-OOM.

> **Hypothesis H3 — warehouse-api degradation**
> **Verdict**: Dismissed
> **Evidence**: `metrics/inventory_warehouse_health.csv` shows `warehouse-api` health probe at 1 (healthy) throughout the incident window. No warehouse-api related error appears in any trace during this period; all trace errors are `gc_pause` on `inventory-svc.stock_lookup` itself, not on the warehouse client.

> **Hypothesis H4 — rds-inventory DB contention**
> **Verdict**: Dismissed
> **Evidence**: All 6 error traces during this incident window trace the error to `inventory-svc.stock_lookup` with `gc_pause` — an in-process error, not a database timeout. `metrics/rds_cpu_pct.csv` (rds-orders) shows no spike during this period; rds-inventory metrics are not separately exposed but no DB-error spans appear in traces.

### 4. Root Cause

**Root cause**: `inventory-svc` v2.5.0 (deployed 2026-03-10 02:00) introduced `numpy 2.0.0`, which changed internal buffer management and caused a memory leak under production workload patterns. RSS grew monotonically over ~22 hours. At 02:15 on 03-11, the `homepage_promo_carousel` feature flag increased frontend traffic, accelerating numpy array allocations. By 02:45, GC pause frequency was high enough to breach latency thresholds. At 04:18, the container was OOM-killed by the kernel, triggering a cold restart that self-resolved the incident.

**Causal chain**:
```
inventory-svc v2.5.0 (numpy 2.0 memory leak)
  → RSS grows monotonically over 22h
    [accelerated by: homepage_promo_carousel flag @ 02:15 → more stock_lookup calls]
      → GC pause latency spikes (03:00 onwards)
        → inventory p99 > 150ms (A-101)
        → checkout-svc: stock_lookup timeouts → error rate > 3% (A-103)
      → RSS crosses 2000 MB (A-102)
        → kernel OOM-kill at 04:18 (A-104)
          → container restart → incident self-resolves
```

### 5. Counterfactual

- **H2 (promo carousel)**: Disabling the carousel flag would have delayed the OOM by reducing allocation rate, but the underlying numpy 2.0 memory leak would have eventually caused the same OOM during the next traffic peak.
- **H3 (warehouse-api)**: Fixing warehouse-api health would have had no effect since warehouse-api was healthy throughout this incident.
- **H4 (rds-inventory)**: No database errors were observed; reducing DB load would not have changed the in-process GC pressure causing the failures.

### 6. Prevention

1. **inventory-svc / dependency upgrades**: Add a memory regression test to the CI pipeline for `inventory-svc`. The test must run a representative production-scale load (minimum 100k stock lookup calls) against any version bump that touches numerical libraries (`numpy`, `pandas`, `scipy`). Pass criterion: RSS growth < 50 MB over 30 minutes of sustained load.
2. **inventory-svc / container resource limits**: Set a memory limit with a pre-OOM alert at 80% of the container limit. When RSS exceeds 80%, trigger a graceful rolling restart of the pod before the kernel OOM-kills it. Measurable: zero unplanned OOM kills in 90 days after implementation.

---

## I-3 · 2026-03-17 11:15 → 12:05

### 1. Timeline

| Timestamp (UTC) | Event | Source |
|---|---|---|
| 09:00 | `payment-svc` TLS cert rotated for pp-api client mTLS (routine, unrelated) | `deploy_log.json` |
| 11:15:00 | Feature flag `enable_loyalty_recommendations` set to enabled at 100% rollout | `deploy_log.json` |
| 11:18:00 | Alert A-201 `CheckoutP99High` fires — p99 > 1000 ms | `alerts.json` A-201 |
| 11:19:00 | Alert A-202 `RdsQueryP99High` fires — query p99 > 1000 ms | `alerts.json` A-202 |
| 11:20 | Trace `97ec012d`: `payment-svc.loyalty_client.recommend` → `rds-orders` query `SELECT * FROM transactions WHERE user_id=? ORDER BY ts DESC`, `rows_examined=180000`, `uses_index=false` | `traces.json` |
| 11:21:00 | Alert A-204 `RdsCpuHigh` fires — CPU > 65% | `alerts.json` A-204 |
| 11:25:00 | Alert A-203 `PaymentConnPoolSaturated` fires — pool > 90% (pool_max=50) | `alerts.json` A-203 |
| 11:27–11:55 | 5 additional traces all show identical pattern: `loyalty_client.recommend` with full-table scan | `traces.json` traces `1cbb16a5`, `95a2790d`, `00e0330f`, `dee7da1c`, `93e08247` |
| ~12:05 | Feature flag rolled back (inferred from metric recovery) | `metrics/rds_query_p99_ms.csv` |

### 2. Candidate Hypotheses

| # | Hypothesis | Confidence (1–5) | Initial reasoning |
|---|---|---|---|
| H1 | `enable_loyalty_recommendations` flag triggered unindexed `rds-orders` full-table scans | 5 | Traces show `uses_index=false`, `rows_examined=180000` exactly 5 min after flag enabled |
| H2 | RDS instance class change (db.r6g.large → xlarge on 03-16) caused instability | 2 | Infrastructure change 20h earlier; could have reset query plan cache |
| H3 | pp-api mTLS cert rotation (09:00) caused payment-svc to hold connections | 1 | Cert rotation is routine; 2+ hours before incident |
| H4 | Increased connection pool utilisation was driven by general traffic spike | 2 | Could explain conn pool saturation without implicating a specific query |

### 3. Evidence Review

> **Hypothesis H1 — loyalty feature flag unindexed query**
> **Verdict**: Accepted
> **Evidence**: Traces starting at 11:20 (5 minutes after the flag was enabled at 11:15) consistently show `payment-svc.loyalty_client.recommend` executing `SELECT * FROM transactions WHERE user_id = ? ORDER BY ts DESC` with `rows_examined=180000` and `uses_index=false`. This full-table scan pattern on a high-cardinality table directly explains: (a) RDS CPU spike (scanning 180k rows per request), (b) query p99 > 1000ms, (c) payment-svc connection pool saturation (slow queries hold DB connections). The 3-minute gap between flag activation and first alert matches expected query-plan compilation and ramp-up time.

> **Hypothesis H2 — RDS instance class change**
> **Verdict**: Dismissed
> **Evidence**: The instance class change from `db.r6g.large` to `db.r6g.xlarge` occurred on 2026-03-16T11:15:00Z — exactly 24 hours before the incident. `metrics/rds_query_p99_ms.csv` and `metrics/rds_cpu_pct.csv` show stable, baseline-level metrics from 03-16 through 03-17 11:15. If the instance change had introduced instability, it would have manifested within minutes of the change, not 24 hours later. Furthermore, a larger instance class would, if anything, provide more capacity.

> **Hypothesis H3 — pp-api mTLS cert rotation**
> **Verdict**: Dismissed
> **Evidence**: The cert rotation occurred at 09:00, more than 2 hours before the incident. Traces during the incident window show errors on `loyalty_client.recommend` (a `rds-orders` call), not on `pp-api` calls. No `mtls_handshake_failure` metric spike appears in `mtls_handshake_failures_per_min.csv` at 11:15.

> **Hypothesis H4 — General traffic spike**
> **Verdict**: Dismissed
> **Evidence**: `metrics/frontend_req_rate.csv` shows no significant traffic increase at 11:15. The conn pool saturation is exclusively explained by slow individual queries (each holding a DB connection for >1000ms), not by volume. The loyalty feature was the only configuration change at 11:15 per `deploy_log.json`.

### 4. Root Cause

**Root cause**: The `enable_loyalty_recommendations` feature flag was activated at 100% rollout at 11:15. The underlying `loyalty_client.recommend` implementation in `payment-svc` executes `SELECT * FROM transactions WHERE user_id = ? ORDER BY ts DESC` against `rds-orders` without a covering index on `(user_id, ts)`. Each call scans approximately 180,000 rows. Under 100% rollout, every checkout request triggered this scan, spiking RDS CPU, driving query p99 above 1000ms, and exhausting the 50-slot connection pool (tighter than I-1's 200-slot pool), which then caused upstream checkout latency and errors.

**Causal chain**:
```
enable_loyalty_recommendations flag enabled at 100% (11:15)
  → payment-svc.loyalty_client.recommend issues unindexed query per checkout
    → rds-orders: 180k rows scanned per call, no index
      → rds_query_p99 > 1000ms (A-202)
      → rds_cpu > 65% (A-204)
        → queries hold DB connections for >1s each
          → payment-svc conn pool (max=50) saturates (A-203)
            → checkout-svc: downstream_timeout on payment-svc
              → checkout p99 > 1000ms (A-201)
```

### 5. Counterfactual

- **H2 (RDS instance change)**: The larger RDS instance would not have prevented the full-table scan; it would merely have absorbed more of the CPU overhead before saturation. With 180k rows per scan × high QPS, even a larger instance would eventually degrade.
- **H3 (mTLS cert rotation)**: A different cert rotation schedule would not have affected the loyalty query code path at all.
- **H4 (traffic spike)**: Even if traffic had been half as high, the unindexed query would still have driven query p99 above the 1000ms threshold at any meaningful scale.

### 6. Prevention

1. **payment-svc / loyalty_client**: Add a mandatory query index policy: before enabling any feature flag that introduces a new DB query in production, the query plan must be reviewed (verified `uses_index=true`) via `EXPLAIN ANALYZE` on a production-sized dataset. Measurable criterion: zero production deploys of feature flags that include `SELECT` queries without index verification sign-off in the PR.
2. **rds-orders / schema**: Add a composite index `CREATE INDEX idx_txn_user_ts ON transactions (user_id, ts DESC)` immediately. Measurable criterion: `rows_examined` for the loyalty query drops from 180,000 to < 100 (index seek), verified by `EXPLAIN` output and `rds_query_p99_ms` < 50ms under the same load.

---

## I-4 · 2026-03-22 09:00 → 09:40

### 1. Timeline

| Timestamp (UTC) | Event | Source |
|---|---|---|
| 2026-03-15 09:00 | Vendor announcement: pp-api IP block rotating `203.0.113.0/24 → 198.51.100.0/24`, TTL=3600, old block removed 2026-03-22 09:00 UTC | `deploy_log.json` |
| 2026-03-20 09:00 | Security team removes legacy egress rule `0.0.0.0/0:443` from `sg-pay-egress` | `deploy_log.json` |
| 09:00:00 | pp-api vendor removes old IP block `203.0.113.0/24` | `deploy_log.json` (vendor announcement) |
| 09:02:00 | Alert A-301 `CheckoutErrorRateHigh` fires — error rate > 3% | `alerts.json` A-301 |
| 09:02 | Trace `b4dd3426`: `payment-svc.charge` (AZ-c) fails with `connection_refused` to `pp.vendor.example` resolving to `203.0.113.10` (old IP) | `traces.json` |
| 09:03:00 | Alert A-302 `PaymentRegionalErrorRateHigh` fires — per-AZ error rate > 20% | `alerts.json` A-302 |
| 09:05:00 | Alert A-303 `PaymentRetriesElevated` fires | `alerts.json` A-303 |
| 09:07 | Trace `a3a3e0fe`: AZ-a checkout succeeds — `payment-svc.charge` resolves to `198.51.100.20` (new IP) | `traces.json` |
| 09:12, 09:22, 09:32 | AZ-c traces continue to fail with `connection_refused` to `203.0.113.10` | `traces.json` |
| 09:17, 09:27, 09:37 | AZ-a/b traces continue to succeed with `198.51.100.20` | `traces.json` |
| ~09:40 | `payment_az_c_error_rate` drops to 0; DNS TTL expires in AZ-c, cache refreshed | `metrics/payment_az_c_error_rate.csv` |

### 2. Candidate Hypotheses

| # | Hypothesis | Confidence (1–5) | Initial reasoning |
|---|---|---|---|
| H1 | AZ-c DNS cache retained old pp-api IP after vendor IP block removal | 5 | Traces show AZ-c resolves to 203.0.113.10 (old); AZ-a/b resolve to 198.51.100.20 (new); vendor announced TTL=3600 |
| H2 | Security group egress rule removal (03-20) blocked outbound pp-api traffic | 3 | Legacy rule `0.0.0.0/0:443` removed 2 days before; could affect certain AZ routing |
| H3 | pp-api experienced a partial outage on 03-22 independent of IP rotation | 2 | Could explain 503s without the DNS angle |
| H4 | payment-svc v3.0.0 (03-09 deploy) introduced a regression in pp-api client | 1 | Deploy was 13 days before; system was stable in between |

### 3. Evidence Review

> **Hypothesis H1 — Stale DNS in AZ-c**
> **Verdict**: Accepted
> **Evidence**: Traces provide definitive AZ-level evidence. Traces from AZ-c (`b4dd3426`, `48f69e70`, `dc55907f`, `8d09fa79`) all show `resolved_ip=203.0.113.10` and `error=connection_refused`. Traces from AZ-a/b (`a3a3e0fe`, `b4f591c7`, `405888ca`, `ff1e6c59`) all show `resolved_ip=198.51.100.20` and succeed. This is a perfect AZ-c isolation pattern matching stale DNS. The vendor announcement (deploy_log `2026-03-15T09:00:00Z`) stated TTL=3600 and old block removal at `2026-03-22 09:00 UTC`, which matches the incident start exactly. Incident resolves ~40 minutes later when the 3600-second TTL expires in AZ-c's resolver.

> **Hypothesis H2 — Security group egress rule removal**
> **Verdict**: Dismissed
> **Evidence**: The security group change (`sg-pay-egress`) occurred 2026-03-20 09:00, two days before the incident. If this had blocked pp-api traffic, failures would have been immediate and would affect all AZs equally (security groups are not AZ-specific per the topology). AZ-a and AZ-b experienced zero failures during this incident, ruling out a security-group-level block.

> **Hypothesis H3 — pp-api partial outage**
> **Verdict**: Dismissed
> **Evidence**: The failing traces show `error=connection_refused` to the specific IP `203.0.113.10`, which no longer exists (removed per vendor announcement). A genuine pp-api outage would show `connection_refused` or 5xx on the *new* IP `198.51.100.20` as well. AZ-a/b traces hitting `198.51.100.20` succeed throughout, confirming pp-api itself is healthy.

> **Hypothesis H4 — payment-svc v3.0.0 regression**
> **Verdict**: Dismissed
> **Evidence**: v3.0.0 deployed on 2026-03-09 and the system was stable for 13 days with zero payment-related alerts in between. Incident start aligns precisely (to the minute) with the vendor-announced IP block removal time.

### 4. Root Cause

**Root cause**: `pp-api`'s vendor rotated their IP block from `203.0.113.0/24` to `198.51.100.0/24` at 09:00 UTC as pre-announced on 2026-03-15. The DNS TTL for `pp.vendor.example` was set to 3600 seconds. AZ-a and AZ-b had refreshed their DNS cache within the TTL window and correctly resolved the new IP. AZ-c's DNS resolver had a cached entry for the old IP (`203.0.113.10`) that had not yet expired. When the old IP block was decommissioned, AZ-c's `payment-svc` pods received `connection_refused` on every pp-api call until the 3600-second TTL expired (~09:40), at which point AZ-c resolved the new IP and payments resumed.

**Causal chain**:
```
pp-api vendor removes 203.0.113.0/24 block @ 09:00
  → AZ-c DNS resolver: stale cache entry for pp.vendor.example → 203.0.113.10
    → payment-svc [AZ-c]: connection_refused to decommissioned IP
      → payment_az_c_error_rate > 20% (A-302)
        → checkout-svc AZ-c errors → overall error rate > 3% (A-301)
          → payment_retries elevated (A-303)
  → AZ-a/b DNS: already resolved to 198.51.100.20 → unaffected
  → DNS TTL expires ~09:40 → AZ-c resolves new IP → self-resolved
```

### 5. Counterfactual

- **H2 (security group)**: Re-adding the `0.0.0.0/0:443` rule would not have helped because the connection was refused at the *destination* (the IP no longer existed), not blocked at the *source* security group.
- **H3 (pp-api outage)**: If pp-api had been independently degraded, failures would have been uniform across all AZs. Restoring pp-api service would not explain the AZ-c-only pattern caused by local DNS caching.
- **H4 (v3.0.0 regression)**: No code change was involved; the failure was entirely at the network/DNS layer. Rolling back payment-svc would not have changed the DNS cache state in AZ-c.

### 6. Prevention

1. **payment-svc / vendor IP rotation process**: Implement a vendor IP-change runbook that, upon receiving an announcement like 2026-03-15's, forces DNS cache invalidation across all AZ resolvers at the time of the change (using `nscd -i hosts` or equivalent). Measurable criterion: zero AZ-isolated payment failures attributable to DNS staleness for future vendor IP rotations.
2. **payment-svc / external dependency monitoring**: Add a pre-incident alerting rule: 72 hours before any announced vendor IP change, trigger a canary check that verifies all AZ-local resolvers return the expected new IP. Fire alert if any AZ still resolves to the old IP at T-1h. Measurable: detection latency < 60 minutes before the rotation deadline.

---

## I-5 · 2026-03-27 06:00 → 06:30

### 1. Timeline

| Timestamp (UTC) | Event | Source |
|---|---|---|
| 05:30:00 | `payment-svc` v3.2.1 deployed (log format change: switch to ECS-compatible field naming) | `deploy_log.json` |
| 06:00:00 | service-mesh-controller begins 24h automatic cert rotation cycle for `checkout-svc → payment-svc` mTLS cert | `deploy_log.json` |
| 06:00:15 | New cert issued with `not_before=2026-03-27T06:00:15Z` | `deploy_log.json` + `traces.json` |
| 06:00:30 | Trace `6fc9a5bb`: `checkout-svc.mtls_client.handshake` fails — `certificate_not_yet_valid`, `not_before=06:00:15Z`, `observed_at_on_validator=05:59:48Z`, `validator_clock_skew_seconds=-27` | `traces.json` |
| 06:00:45 | Alert A-401 `MtlsHandshakeFailureSpike` fires — > 100/min | `alerts.json` A-401 |
| 06:01:00 | Alert A-402 `CheckoutErrorRateHigh` fires — error rate > 20% | `alerts.json` A-402 |
| 06:02:00 | Alert A-403 `SyntheticCheckoutFailing` fires | `alerts.json` A-403 |
| 06:02–06:08 | Additional traces all confirm: `certificate_not_yet_valid` with same `validator_clock_skew_seconds=-27` | `traces.json` traces `bcb41ec8`, `477e7526`, `61222ca2`, `88b2f53d`, `e8996494` |
| ~06:30 | `mtls_handshake_failures_per_min` returns to 0; validator clock re-synchronized or cert TTL window passed | `metrics/mtls_handshake_failures_per_min.csv` |

### 2. Candidate Hypotheses

| # | Hypothesis | Confidence (1–5) | Initial reasoning |
|---|---|---|---|
| H1 | Validator clock skew (-27s) caused new cert to appear not-yet-valid during the rotation window | 5 | All traces show `validator_clock_skew_seconds=-27` and `not_before=06:00:15Z`; validator clock shows 05:59:48 when cert is valid from 06:00:15 |
| H2 | payment-svc v3.2.1 deploy (05:30) introduced a TLS regression | 3 | Deploy 30 min before incident; log format changes could have touched TLS config |
| H3 | Service mesh automatically rotating certs caused a brief window with no valid cert (cert gap) | 2 | Cert rotation could in theory leave a gap before new cert is distributed |
| H4 | The new cert was mis-issued with wrong SANs or validity period | 1 | Could cause handshake failure, but would persist beyond 30 min |

### 3. Evidence Review

> **Hypothesis H1 — Validator clock skew**
> **Verdict**: Accepted
> **Evidence**: All 7 error traces share an identical set of fields: `not_before=2026-03-27T06:00:15Z`, `observed_at_on_validator=2026-03-27T05:59:48Z`, `validator_clock_skew_seconds=-27`. The validator's clock is running 27 seconds behind UTC. When the cert was issued at 06:00:15Z, the validator saw the current time as 05:59:48Z, which is 27 seconds *before* `not_before`. Hence every handshake attempt rejected the cert as "not yet valid." The incident resolved at ~06:30 when the validator's clock caught up sufficiently (or NTP sync re-established), placing the local time past the `not_before` boundary.

> **Hypothesis H2 — payment-svc v3.2.1 TLS regression**
> **Verdict**: Dismissed
> **Evidence**: The failing side is `checkout-svc.mtls_client.handshake` (the *initiating* side), not `payment-svc`. The error occurs at the *validator* on checkout-svc's clock, not on payment-svc. Additionally, the v3.2.1 changes were limited to log format (ECS field naming), which does not touch TLS configuration. Traces show the cert itself is valid (`not_before`, `not_after` values are well-formed); the issue is purely the validator's local clock.

> **Hypothesis H3 — Cert distribution gap**
> **Verdict**: Dismissed
> **Evidence**: A cert distribution gap would produce `certificate_expired` or `unknown_certificate` errors, not `certificate_not_yet_valid`. The trace error is specifically `certificate_not_yet_valid`, indicating the cert was *received and parsed correctly* but its validity window had not yet opened according to the validator's clock. The `not_before` timestamp in all traces is consistent and matches the rotation event in deploy_log.

> **Hypothesis H4 — Mis-issued cert**
> **Verdict**: Dismissed
> **Evidence**: All traces show the same `not_before=2026-03-27T06:00:15Z` — a plausible issuance time 15 seconds after the rotation cycle began at 06:00:00. If the SANs or key material were wrong, the error type would be different (e.g., `certificate_verify_failed`, `hostname_mismatch`) and the incident would not have self-resolved within 30 minutes.

### 4. Root Cause

**Root cause**: The automatic 24h mTLS cert rotation for the `checkout-svc → payment-svc` link issued a new certificate with `not_before=2026-03-27T06:00:15Z`. The NTP clock on the `checkout-svc` mTLS validator was running 27 seconds slow (`05:59:48Z` observed vs `06:00:15Z` cert not_before). Every handshake attempt within this ~27-second window (and longer due to NTP drift not recovering immediately) was rejected with `certificate_not_yet_valid`. The incident self-resolved when the validator's clock advanced past the `not_before` boundary (~06:27 based on the 27s drift correcting over the window).

**Causal chain**:
```
service-mesh rotates checkout-svc → payment-svc mTLS cert @ 06:00:00
  → new cert issued: not_before=06:00:15Z
    → checkout-svc mTLS validator: clock=-27s → local time reads 05:59:48Z
      → 05:59:48Z < 06:00:15Z → certificate_not_yet_valid
        → all checkout → payment-svc mTLS handshakes fail
          → mtls_handshake_failures > 100/min (A-401)
          → checkout error rate > 20% (A-402)
          → SyntheticCheckoutFailing (A-403)
  → ~06:30: validator clock advances past not_before → self-resolves
```

### 5. Counterfactual

- **H2 (v3.2.1 deploy)**: Rolling back payment-svc to v3.2.0 would not have affected the validator's clock state on checkout-svc; handshake failures would have continued.
- **H3 (cert distribution gap)**: Speeding up cert distribution would not help when the problem is the validator's clock — a faster-distributed cert would fail on the same `not_before` check.
- **H4 (mis-issued cert)**: Even a correctly re-issued cert with an earlier `not_before` would be a workaround, not a fix — the underlying NTP drift on the validator would affect the next rotation cycle as well.

### 6. Prevention

1. **service-mesh / cert rotation policy**: Add a 60-second `not_before` grace period to all automatically rotated mTLS certs (i.e., set `not_before = rotation_time - 60s`). This absorbs any validator clock skew up to 60 seconds. Measurable criterion: zero `certificate_not_yet_valid` errors during cert rotation events across all services for 90 days.
2. **all nodes / NTP monitoring**: Add a continuous NTP offset monitoring alert: if any node's `chronyc tracking` or `timedatectl` shows offset > 10 seconds, page on-call immediately. Measurable criterion: maximum observed clock offset < 5 seconds across all nodes at all times, verified by a per-minute cron job.

---

## Non-incident Events Ruled Out

### NI-1 · 2026-03-14 02:15 (Alert A-501)

- **Time window**: 2026-03-14 02:00–03:00 UTC
- **Signal that drew attention**: Alert A-501 `SyntheticCheckoutFailing` (warning severity) fired at 02:15 — synthetic check showing elevated latency.
- **Evidence ruling it out**: `deploy_log.json` entry on 2026-03-13T18:00:00Z by `dba-team` explicitly states: *"scheduled one-time full DB backup: window 2026-03-14 02:00-03:00 UTC; expect elevated read replica lag and synthetic check noise"*, approved in change board CR-2026-0287. The alert is a single warning with no child alerts or correlated metric spikes in `checkout_error_rate.csv` or `checkout_p99_ms.csv`. This is planned maintenance noise, not an incident.

### NI-2 · 2026-03-01 09:30 (Redis rolling restart)

- **Time window**: 2026-03-01 09:30–10:00 UTC
- **Signal that drew attention**: `deploy_log.json` records a `rolling restart for kernel patch` on `redis-cache` at 09:30, which would typically cause transient cache evictions and a hit-rate dip. A naive scan of `redis_evictions_per_min.csv` might flag this as an anomaly.
- **Evidence ruling it out**: No alerts fired during this window. `metrics/redis_hit_rate.csv` shows a brief, contained dip that recovers within minutes — consistent with a planned rolling restart (nodes come back one at a time, not all at once). `metrics/checkout_p99_ms.csv` and `checkout_error_rate.csv` show no corresponding degradation. The event is logged as `type=maintenance` with `actor=platform-team`, matching the expected pattern.

### NI-3 · 2026-03-17 09:00 (pp-api mTLS cert rotation)

- **Time window**: 2026-03-17 09:00–09:15 UTC
- **Signal that drew attention**: `deploy_log.json` shows a `TLS cert rotated for pp-api client mTLS` at 09:00. Given the mTLS incident I-5, this looks suspicious.
- **Evidence ruling it out**: No alerts fired at 09:00 on 03-17. `metrics/mtls_handshake_failures_per_min.csv` shows no spike. `metrics/checkout_error_rate.csv` is flat. The cert rotation completed without clock skew issues — all services at the time had synchronized clocks. The I-5 clock-skew condition did not exist on 03-17 (the NTP drift only appears in I-5 traces on 03-27).
