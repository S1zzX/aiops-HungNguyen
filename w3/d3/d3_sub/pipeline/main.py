"""AIOps Pipeline — FastAPI service exposing /alerts, /correlate, /rca.

Simulates a W1+W2 style detector + correlator + RCA engine.
Pulls metrics from Prometheus and generates alerts + root cause analysis.
"""
import time
import random
import os
from fastapi import FastAPI, Query
from pydantic import BaseModel
import httpx

app = FastAPI(title="AIOps Pipeline", version="1.0.0")

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")

# In-memory alert store — populated by /ingest or background scrape
_alerts: list[dict] = []

# Topology graph: service -> upstream dependencies
TOPOLOGY = {
    "frontend":        ["api-gateway"],
    "api-gateway":     ["payment-svc", "inventory-svc", "checkout-svc", "auth-svc", "notification-svc"],
    "checkout-svc":    ["payment-svc", "inventory-svc"],
    "payment-svc":     ["payment-db"],
    "inventory-svc":   ["inventory-db"],
    "notification-svc": [],
    "auth-svc":        [],
    "log-collector":   [],
    "dns-resolver":    [],
    "cache-svc":       [],
    "payment-db":      [],
    "inventory-db":    [],
}

# Known fault signatures: maps service -> expected alert labels
FAULT_SIGNATURES = {
    "payment-svc":    ["latency", "error_rate", "network_loss"],
    "inventory-svc":  ["availability", "restart_count"],
    "api-gateway":    ["cpu_saturation", "cascade_latency"],
    "payment-db":     ["memory", "connection_pool"],
    "auth-svc":       ["time_skew", "jwt_failure"],
    "log-collector":  ["disk_fill", "ingestion_lag"],
    "dns-resolver":   ["dns_latency", "intermittent_error"],
    "checkout-svc":   ["http_error", "retry_storm"],
}


# ── Models ───────────────────────────────────────────────────────────────────

class CorrelateRequest(BaseModel):
    window_start: int
    window_end: int


class RCARequest(BaseModel):
    window_start: int
    window_end: int


class IngestAlert(BaseModel):
    service: str
    fault_class: str
    severity: str = "warning"
    fire_ts: int | None = None


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "ts": int(time.time())}


@app.post("/ingest")
def ingest_alert(alert: IngestAlert):
    """Receive an alert from an external source (chaos runner, alertmanager, etc.)."""
    entry = {
        "service": alert.service,
        "fault_class": alert.fault_class,
        "severity": alert.severity,
        "fire_ts": alert.fire_ts or int(time.time()),
    }
    _alerts.append(entry)
    return {"status": "ingested", "alert": entry}


@app.get("/alerts")
def get_alerts(since: int = Query(default=0)):
    """Return all alerts fired since a given Unix timestamp."""
    # Also try to pull from Prometheus for real metrics
    prom_alerts = _pull_prometheus_alerts()
    all_alerts = _alerts + prom_alerts
    filtered = [a for a in all_alerts if a.get("fire_ts", 0) >= since]
    return filtered


@app.post("/correlate")
def correlate(req: CorrelateRequest):
    """Cluster alerts in a time window into incident groups."""
    window_alerts = [
        a for a in _alerts
        if req.window_start <= a.get("fire_ts", 0) <= req.window_end
    ]
    # Simple topology-aware clustering: group by upstream dependency chain
    clusters = _cluster_by_topology(window_alerts)
    return {"clusters": clusters, "window_start": req.window_start, "window_end": req.window_end}


@app.post("/rca")
def rca(req: RCARequest):
    """Root cause analysis for alerts in a time window."""
    window_alerts = [
        a for a in _alerts
        if req.window_start <= a.get("fire_ts", 0) <= req.window_end
    ]

    if not window_alerts:
        return {
            "root_service": None,
            "confidence": 0.0,
            "evidence": [],
            "reasoning": "No alerts in window",
        }

    root_service, confidence, evidence = _topology_rca(window_alerts)

    return {
        "root_service": root_service,
        "confidence": confidence,
        "evidence": evidence,
        "reasoning": f"Topology-aware RCA: traced alert chain upstream to {root_service}",
    }


@app.delete("/alerts/clear")
def clear_alerts():
    """Clear in-memory alert store (used between experiments)."""
    _alerts.clear()
    return {"status": "cleared"}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _pull_prometheus_alerts() -> list[dict]:
    """Try to query Prometheus for anomalies. Returns empty list on failure."""
    try:
        queries = {
            "latency":    'histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[1m])) > 0.5',
            "error_rate": 'rate(http_requests_total{status=~"5.."}[1m]) > 0.01',
            "cpu":        'rate(process_cpu_seconds_total[1m]) > 0.8',
        }
        alerts = []
        with httpx.Client(timeout=3.0) as client:
            for fault_class, query in queries.items():
                resp = client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query})
                if resp.status_code == 200:
                    data = resp.json().get("data", {}).get("result", [])
                    for r in data:
                        service = r.get("metric", {}).get("job", "unknown")
                        alerts.append({
                            "service": service,
                            "fault_class": fault_class,
                            "severity": "warning",
                            "fire_ts": int(time.time()),
                            "source": "prometheus",
                        })
        return alerts
    except Exception:
        return []


def _cluster_by_topology(alerts: list[dict]) -> list[dict]:
    """Group alerts by their position in the dependency topology."""
    if not alerts:
        return []
    # Simple: one cluster per unique upstream chain
    seen = set()
    clusters = []
    for alert in alerts:
        svc = alert["service"]
        if svc not in seen:
            seen.add(svc)
            upstream = TOPOLOGY.get(svc, [])
            clusters.append({
                "root_candidate": svc,
                "members": [svc] + upstream,
                "alert_count": sum(1 for a in alerts if a["service"] in [svc] + upstream),
            })
    return clusters


def _topology_rca(alerts: list[dict]) -> tuple[str, float, list[str]]:
    """
    Topology-aware RCA:
    1. Count alerts per service
    2. Walk topology upstream — prefer services with no upstream (true roots)
    3. Avoid picking a service that is downstream-only (retry-storm trap)
    """
    from collections import Counter

    alert_counts = Counter(a["service"] for a in alerts)
    services_with_alerts = set(alert_counts.keys())

    # Find services that are NOT downstream of any other alerted service
    # i.e., no other alerted service depends on them
    def is_downstream_of_alerted(svc: str) -> bool:
        """Return True if svc is a downstream symptom of another alerted service."""
        for other_svc in services_with_alerts:
            if other_svc == svc:
                continue
            # Check if svc appears in upstream of other_svc (meaning other_svc depends on svc)
            # We want to know if svc is downstream: does svc depend on an alerted service?
            if other_svc in TOPOLOGY.get(svc, []):
                return True  # svc depends on other_svc → other_svc is more root
        return False

    # Rank: prefer services that are NOT downstream of another alerted service
    root_candidates = []
    symptom_candidates = []

    for svc in services_with_alerts:
        if is_downstream_of_alerted(svc):
            symptom_candidates.append(svc)
        else:
            root_candidates.append(svc)

    if root_candidates:
        # Among root candidates, pick the one with fewest upstream (deepest in stack)
        root_candidates.sort(key=lambda s: len(TOPOLOGY.get(s, [])))
        chosen = root_candidates[0]
        confidence = min(0.95, 0.6 + 0.1 * alert_counts[chosen])
    else:
        # Fallback: pick highest alert count (less accurate)
        chosen = alert_counts.most_common(1)[0][0]
        confidence = 0.4

    evidence = [
        f"{a['service']}: {a['fault_class']} at ts={a['fire_ts']}"
        for a in alerts if a["service"] == chosen
    ]

    return chosen, round(confidence, 2), evidence
