"""Generate realistic eval incidents E01-E08 based on handout spec."""
import json, random
from pathlib import Path

TOPOLOGY = json.load(open("/home/claude/lab/topology.json"))

def ts(base="2026-06-10T14:23:00Z"):
    return base

def make_metrics(service, metric, v_before, v_after, n=80):
    import time
    samples = []
    for i in range(n):
        frac = i / n
        v = v_before + (v_after - v_before) * max(0, frac - 0.5) * 2
        samples.append([f"2026-06-10T{13+int(i*37/n//60):02d}:{int(i*37/n)%60:02d}:00Z", round(v, 1)])
    return {f"{service}.{metric}": samples}

# E01 - connection_pool_exhaustion on payment-svc (easy, must NOT page_oncall)
E01 = {
    "incident_id": "E01",
    "detected_at": "2026-06-10T14:23:00Z",
    "trigger_alert": {"service": "checkout-svc", "rule_id": "latency-p99-high", "severity": "critical"},
    "topology": TOPOLOGY,
    "metrics_window": {
        "from": "2026-06-10T13:53:00Z", "to": "2026-06-10T14:30:00Z",
        "samples": {
            **make_metrics("payment-svc", "conn_pool_used", 30, 99),
            **make_metrics("payment-svc", "latency_p99_ms", 120, 2400),
            **make_metrics("checkout-svc", "latency_p99_ms", 150, 2800),
        }
    },
    "traces": [
        {"ts": "2026-06-10T14:23:05Z", "from": "checkout-svc", "to": "payment-svc",
         "count": 71, "error_count": 44, "p50_ms": 280, "p99_ms": 2410},
        {"ts": "2026-06-10T14:22:00Z", "from": "edge-lb", "to": "checkout-svc",
         "count": 200, "error_count": 60, "p50_ms": 300, "p99_ms": 2600},
    ],
    "logs": [
        {"ts": "2026-06-10T14:22:51Z", "svc": "payment-svc", "level": "ERROR",
         "msg": "ConnectionPool: timeout acquiring connection (waited 5000ms)"},
        {"ts": "2026-06-10T14:22:52Z", "svc": "payment-svc", "level": "ERROR",
         "msg": "Failed to forward request: pool exhausted"},
        {"ts": "2026-06-10T14:22:53Z", "svc": "checkout-svc", "level": "WARN",
         "msg": "Upstream payment-svc returning 503"},
    ] + [{"ts": "2026-06-10T14:23:00Z", "svc": "payment-svc", "level": "ERROR",
          "msg": "ConnectionPool: timeout acquiring connection (waited 5000ms)"} for _ in range(20)]
}

# E02 - bad deploy on catalog-svc (easy)
E02 = {
    "incident_id": "E02",
    "detected_at": "2026-06-10T10:15:00Z",
    "trigger_alert": {"service": "catalog-svc", "rule_id": "error-rate-high", "severity": "critical"},
    "topology": TOPOLOGY,
    "metrics_window": {
        "from": "2026-06-10T09:45:00Z", "to": "2026-06-10T10:30:00Z",
        "samples": {
            **make_metrics("catalog-svc", "error_rate", 0.01, 0.35),
            **make_metrics("catalog-svc", "latency_p99_ms", 200, 1800),
        }
    },
    "traces": [
        {"ts": "2026-06-10T10:15:05Z", "from": "cart-svc", "to": "catalog-svc",
         "count": 100, "error_count": 35, "p50_ms": 400, "p99_ms": 1800},
        {"ts": "2026-06-10T10:15:10Z", "from": "edge-lb", "to": "catalog-svc",
         "count": 150, "error_count": 50, "p50_ms": 380, "p99_ms": 1700},
    ],
    "logs": [
        {"ts": "2026-06-10T10:14:00Z", "svc": "catalog-svc", "level": "ERROR",
         "msg": "NullPointerException in ProductController.listProducts after deploy v2.4.1"},
        {"ts": "2026-06-10T10:14:05Z", "svc": "catalog-svc", "level": "ERROR",
         "msg": "Unhandled exception in request handler: deploy v2.4.1"},
        {"ts": "2026-06-10T10:13:55Z", "svc": "catalog-svc", "level": "INFO",
         "msg": "New deployment detected: catalog-svc v2.4.1 rolled out"},
    ] + [{"ts": "2026-06-10T10:15:00Z", "svc": "catalog-svc", "level": "ERROR",
          "msg": "Unhandled exception in request handler: deploy v2.4.1"} for _ in range(15)]
}

# E03 - memory leak on recommender-svc (easy)
E03 = {
    "incident_id": "E03",
    "detected_at": "2026-06-10T09:00:00Z",
    "trigger_alert": {"service": "recommender-svc", "rule_id": "oom-kill", "severity": "critical"},
    "topology": TOPOLOGY,
    "metrics_window": {
        "from": "2026-06-10T08:30:00Z", "to": "2026-06-10T09:15:00Z",
        "samples": {
            **make_metrics("recommender-svc", "memory_used_mb", 200, 980),
            **make_metrics("recommender-svc", "latency_p99_ms", 80, 3200),
        }
    },
    "traces": [
        {"ts": "2026-06-10T09:00:05Z", "from": "catalog-svc", "to": "recommender-svc",
         "count": 50, "error_count": 30, "p50_ms": 1200, "p99_ms": 8000},
    ],
    "logs": [
        {"ts": "2026-06-10T08:58:00Z", "svc": "recommender-svc", "level": "ERROR",
         "msg": "OOMKilled: memory limit exceeded, container restarting"},
        {"ts": "2026-06-10T08:58:05Z", "svc": "recommender-svc", "level": "WARN",
         "msg": "Heap usage above 90% threshold, possible memory leak"},
        {"ts": "2026-06-10T08:57:00Z", "svc": "recommender-svc", "level": "ERROR",
         "msg": "GC overhead limit exceeded in model inference loop"},
    ] + [{"ts": "2026-06-10T09:00:00Z", "svc": "recommender-svc", "level": "ERROR",
          "msg": "OOMKilled: memory limit exceeded, container restarting"} for _ in range(10)]
}

# E04 - lock contention on payments-db (easy)
E04 = {
    "incident_id": "E04",
    "detected_at": "2026-06-10T16:45:00Z",
    "trigger_alert": {"service": "payment-svc", "rule_id": "latency-p99-high", "severity": "high"},
    "topology": TOPOLOGY,
    "metrics_window": {
        "from": "2026-06-10T16:15:00Z", "to": "2026-06-10T17:00:00Z",
        "samples": {
            **make_metrics("payments-db", "lock_wait_ms", 5, 4800),
            **make_metrics("payment-svc", "latency_p99_ms", 100, 3500),
        }
    },
    "traces": [
        {"ts": "2026-06-10T16:45:05Z", "from": "payment-svc", "to": "payments-db",
         "count": 80, "error_count": 12, "p50_ms": 900, "p99_ms": 4800},
    ],
    "logs": [
        {"ts": "2026-06-10T16:44:00Z", "svc": "payments-db", "level": "ERROR",
         "msg": "Lock wait timeout exceeded; try restarting transaction"},
        {"ts": "2026-06-10T16:44:05Z", "svc": "payments-db", "level": "ERROR",
         "msg": "Deadlock found when trying to get lock; try restarting transaction"},
        {"ts": "2026-06-10T16:44:10Z", "svc": "payment-svc", "level": "WARN",
         "msg": "DB query timeout after 5000ms, retrying"},
    ] + [{"ts": "2026-06-10T16:45:00Z", "svc": "payments-db", "level": "ERROR",
          "msg": "Lock wait timeout exceeded; try restarting transaction"} for _ in range(18)]
}

# E05 - two history incidents tie (medium) - connection_pool_exhaustion vs bad_deploy both close
E05 = {
    "incident_id": "E05",
    "detected_at": "2026-06-10T11:30:00Z",
    "trigger_alert": {"service": "checkout-svc", "rule_id": "error-rate-high", "severity": "critical"},
    "topology": TOPOLOGY,
    "metrics_window": {
        "from": "2026-06-10T11:00:00Z", "to": "2026-06-10T12:00:00Z",
        "samples": {
            **make_metrics("payment-svc", "conn_pool_used", 28, 95),
            **make_metrics("payment-svc", "latency_p99_ms", 110, 2200),
            **make_metrics("checkout-svc", "latency_p99_ms", 140, 2500),
        }
    },
    "traces": [
        {"ts": "2026-06-10T11:30:05Z", "from": "checkout-svc", "to": "payment-svc",
         "count": 65, "error_count": 40, "p50_ms": 260, "p99_ms": 2300},
    ],
    "logs": [
        {"ts": "2026-06-10T11:29:51Z", "svc": "payment-svc", "level": "ERROR",
         "msg": "ConnectionPool: timeout acquiring connection (waited 5000ms)"},
        {"ts": "2026-06-10T11:29:52Z", "svc": "payment-svc", "level": "ERROR",
         "msg": "Failed to forward request: pool exhausted"},
        # Also has deploy noise
        {"ts": "2026-06-10T11:28:00Z", "svc": "payment-svc", "level": "INFO",
         "msg": "Deployment event: payment-svc v4.2.0 canary rollout"},
        {"ts": "2026-06-10T11:29:00Z", "svc": "checkout-svc", "level": "WARN",
         "msg": "Upstream payment-svc returning 503"},
    ] + [{"ts": "2026-06-10T11:30:00Z", "svc": "payment-svc", "level": "ERROR",
          "msg": "ConnectionPool: timeout acquiring connection (waited 5000ms)"} for _ in range(18)]
}

# E06 - conflicting evidence: logs say payment-svc, traces say checkout-svc (hard)
E06 = {
    "incident_id": "E06",
    "detected_at": "2026-06-10T15:00:00Z",
    "trigger_alert": {"service": "checkout-svc", "rule_id": "latency-p99-high", "severity": "critical"},
    "topology": TOPOLOGY,
    "metrics_window": {
        "from": "2026-06-10T14:30:00Z", "to": "2026-06-10T15:15:00Z",
        "samples": {
            **make_metrics("checkout-svc", "latency_p99_ms", 150, 3000),
            **make_metrics("payment-svc", "conn_pool_used", 45, 92),
            **make_metrics("cart-svc", "latency_p99_ms", 80, 100),  # mostly fine
        }
    },
    "traces": [
        # Traces blame payment-svc strongly
        {"ts": "2026-06-10T15:00:05Z", "from": "checkout-svc", "to": "payment-svc",
         "count": 90, "error_count": 58, "p50_ms": 350, "p99_ms": 3100},
        {"ts": "2026-06-10T15:00:06Z", "from": "checkout-svc", "to": "cart-svc",
         "count": 90, "error_count": 2, "p50_ms": 80, "p99_ms": 200},  # cart-svc fine
    ],
    "logs": [
        # Logs mention checkout-svc issues (misleading)
        {"ts": "2026-06-10T14:59:00Z", "svc": "checkout-svc", "level": "ERROR",
         "msg": "Checkout pipeline timeout: downstream dependency slow"},
        {"ts": "2026-06-10T14:59:05Z", "svc": "payment-svc", "level": "ERROR",
         "msg": "ConnectionPool: timeout acquiring connection (waited 5000ms)"},
        {"ts": "2026-06-10T14:59:10Z", "svc": "payment-svc", "level": "ERROR",
         "msg": "Failed to forward request: pool exhausted"},
    ] + [{"ts": "2026-06-10T15:00:00Z", "svc": "payment-svc", "level": "ERROR",
          "msg": "ConnectionPool: timeout acquiring connection (waited 5000ms)"} for _ in range(22)]
}

# E07 - novel pattern: unusual DNS / network issue never seen before (hard, must page_oncall)
E07 = {
    "incident_id": "E07",
    "detected_at": "2026-06-10T08:00:00Z",
    "trigger_alert": {"service": "auth-svc", "rule_id": "connection-refused", "severity": "critical"},
    "topology": TOPOLOGY,
    "metrics_window": {
        "from": "2026-06-10T07:30:00Z", "to": "2026-06-10T08:15:00Z",
        "samples": {
            **make_metrics("auth-svc", "dns_lookup_ms", 5, 8000),
            **make_metrics("auth-svc", "latency_p99_ms", 90, 15000),
            **make_metrics("edge-lb", "latency_p99_ms", 80, 12000),
        }
    },
    "traces": [
        {"ts": "2026-06-10T08:00:05Z", "from": "edge-lb", "to": "auth-svc",
         "count": 200, "error_count": 190, "p50_ms": 8000, "p99_ms": 15000},
    ],
    "logs": [
        # DNS / mTLS / certificate rotation - totally novel pattern
        {"ts": "2026-06-10T07:59:00Z", "svc": "auth-svc", "level": "ERROR",
         "msg": "NXDOMAIN: unable to resolve identity-provider.internal after 3 retries"},
        {"ts": "2026-06-10T07:59:05Z", "svc": "auth-svc", "level": "ERROR",
         "msg": "mTLS handshake failed: certificate chain validation error CN=identity-provider.internal"},
        {"ts": "2026-06-10T07:59:10Z", "svc": "auth-svc", "level": "ERROR",
         "msg": "SPIFFE SVID rotation failed: upstream unavailable"},
        {"ts": "2026-06-10T07:58:50Z", "svc": "auth-svc", "level": "WARN",
         "msg": "DNS resolution intermittent: identity-provider.internal returning SERVFAIL"},
    ] + [{"ts": "2026-06-10T08:00:00Z", "svc": "auth-svc", "level": "ERROR",
          "msg": "NXDOMAIN: unable to resolve identity-provider.internal"} for _ in range(15)]
}

# E08 - cascade: catalog-db slow query causes catalog-svc -> cart-svc -> checkout-svc (hard)
E08 = {
    "incident_id": "E08",
    "detected_at": "2026-06-10T13:00:00Z",
    "trigger_alert": {"service": "checkout-svc", "rule_id": "latency-p99-high", "severity": "critical"},
    "topology": TOPOLOGY,
    "metrics_window": {
        "from": "2026-06-10T12:30:00Z", "to": "2026-06-10T13:15:00Z",
        "samples": {
            **make_metrics("checkout-svc", "latency_p99_ms", 160, 4200),
            **make_metrics("cart-svc", "latency_p99_ms", 90, 3800),
            **make_metrics("catalog-svc", "latency_p99_ms", 200, 5800),
            **make_metrics("catalog-db", "query_latency_p99_ms", 50, 6200),
            **make_metrics("recommender-svc", "latency_p99_ms", 100, 5500),
            **make_metrics("search-svc", "latency_p99_ms", 90, 5100),
        }
    },
    "traces": [
        # The root is catalog-db, but alert fires on checkout-svc
        {"ts": "2026-06-10T13:00:05Z", "from": "checkout-svc", "to": "cart-svc",
         "count": 100, "error_count": 40, "p50_ms": 1800, "p99_ms": 4200},
        {"ts": "2026-06-10T13:00:06Z", "from": "cart-svc", "to": "catalog-svc",
         "count": 100, "error_count": 38, "p50_ms": 1600, "p99_ms": 3900},
        {"ts": "2026-06-10T13:00:07Z", "from": "catalog-svc", "to": "catalog-db",
         "count": 200, "error_count": 5, "p50_ms": 3000, "p99_ms": 6200},  # high latency, root!
        {"ts": "2026-06-10T13:00:08Z", "from": "catalog-svc", "to": "recommender-svc",
         "count": 80, "error_count": 30, "p50_ms": 1400, "p99_ms": 5500},
        {"ts": "2026-06-10T13:00:09Z", "from": "search-svc", "to": "catalog-db",
         "count": 90, "error_count": 4, "p50_ms": 2900, "p99_ms": 6000},
    ],
    "logs": [
        {"ts": "2026-06-10T12:59:00Z", "svc": "catalog-db", "level": "ERROR",
         "msg": "query took longer than threshold: SELECT * FROM products WHERE category_id=? took 6120ms"},
        {"ts": "2026-06-10T12:59:05Z", "svc": "catalog-db", "level": "ERROR",
         "msg": "DB query latency > 5s on table products, sequential scan detected"},
        {"ts": "2026-06-10T12:59:10Z", "svc": "catalog-svc", "level": "WARN",
         "msg": "Upstream catalog-db query timeout after 5000ms"},
        {"ts": "2026-06-10T12:59:15Z", "svc": "cart-svc", "level": "WARN",
         "msg": "Upstream catalog-svc timeout"},
        {"ts": "2026-06-10T12:59:20Z", "svc": "checkout-svc", "level": "WARN",
         "msg": "cart-svc dependency slow, circuit breaker at 40% open"},
    ] + [{"ts": "2026-06-10T13:00:00Z", "svc": "catalog-db", "level": "ERROR",
          "msg": "query took longer than threshold: SELECT * FROM products sequential scan"} for _ in range(20)]
}

# Write all eval files
evals = [E01, E02, E03, E04, E05, E06, E07, E08]
for e in evals:
    path = Path(f"/home/claude/lab/eval/{e['incident_id']}.json")
    path.write_text(json.dumps(e, indent=2))
    print(f"Wrote {path}")

# Write expected.json
expected = {
    "E01": {
        "accepted_actions": [
            {"name": "rollback_service", "params": {"service": "payment-svc"}},
            {"name": "increase_pool_size", "params": {"service": "payment-svc"}}
        ],
        "must_not_action": "page_oncall",
        "notes": "connection_pool_exhaustion on payment-svc — must NOT escalate"
    },
    "E02": {
        "accepted_actions": [
            {"name": "rollback_service", "params": {"service": "catalog-svc"}}
        ],
        "notes": "bad deploy on catalog-svc"
    },
    "E03": {
        "accepted_actions": [
            {"name": "rollback_service", "params": {"service": "recommender-svc"}},
            {"name": "restart_pod", "params": {"service": "recommender-svc"}}
        ],
        "notes": "memory leak / OOM on recommender-svc"
    },
    "E04": {
        "accepted_actions": [
            {"name": "restart_pod", "params": {"service": "payments-db"}}
        ],
        "notes": "lock contention on payments-db"
    },
    "E05": {
        "accepted_actions": [
            {"name": "increase_pool_size", "params": {"service": "payment-svc"}},
            {"name": "rollback_service", "params": {"service": "payment-svc"}}
        ],
        "notes": "connection pool exhaustion, outcome-weighted voting should pick increase_pool_size"
    },
    "E06": {
        "accepted_actions": [
            {"name": "increase_pool_size", "params": {"service": "payment-svc"}},
            {"name": "rollback_service", "params": {"service": "payment-svc"}}
        ],
        "notes": "conflicting evidence — trace signal dominates, payment-svc pool exhaustion is root"
    },
    "E07": {
        "accepted_actions": [{"name": "page_oncall", "params": {"team": "platform-team"}}],
        "notes": "OOD novel DNS/mTLS pattern — must escalate, do not auto-act"
    },
    "E08": {
        "accepted_actions": [
            {"name": "page_oncall", "params": {"team": "platform-team"}}
        ],
        "notes": "cascade with leaf root (catalog-db slow_query) — page_oncall acceptable; engine may also suggest restart_pod:catalog-db"
    }
}

Path("/home/claude/lab/eval/expected.json").write_text(json.dumps(expected, indent=2))
print("Wrote expected.json")
