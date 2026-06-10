"""Unit tests for correlate.py — pure functions, no external deps."""
import networkx as nx
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from correlate import build_graph_from_json, correlate, fingerprint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_alert(service, ts, metric="latency_p99_ms", severity="crit", value=1000.0):
    return {
        "id": f"{service}-{ts}",
        "ts": ts,
        "service": service,
        "metric": metric,
        "severity": severity,
        "value": value,
        "threshold": 800.0,
    }


def _simple_graph():
    G = nx.DiGraph()
    G.add_edge("payment-svc", "db-primary")
    G.add_edge("order-svc", "payment-svc")
    G.add_edge("api-gateway", "order-svc")
    return G


# ---------------------------------------------------------------------------
# fingerprint tests
# ---------------------------------------------------------------------------

def test_fingerprint_excludes_timestamp():
    a = _make_alert("payment-svc", "2026-06-12T09:42:01Z", value=1840)
    b = _make_alert("payment-svc", "2026-06-12T09:42:30Z", value=1900)
    assert fingerprint(a) == fingerprint(b), "Same service+metric+severity → same fingerprint regardless of ts/value"


def test_fingerprint_differs_by_service():
    a = _make_alert("payment-svc", "2026-06-12T09:42:01Z")
    b = _make_alert("order-svc", "2026-06-12T09:42:01Z")
    assert fingerprint(a) != fingerprint(b)


def test_fingerprint_differs_by_severity():
    a = _make_alert("payment-svc", "2026-06-12T09:42:01Z", severity="crit")
    b = _make_alert("payment-svc", "2026-06-12T09:42:01Z", severity="warn")
    assert fingerprint(a) != fingerprint(b)


# ---------------------------------------------------------------------------
# correlate tests
# ---------------------------------------------------------------------------

def test_correlate_empty_returns_empty():
    G = _simple_graph()
    assert correlate([], G) == []


def test_correlate_single_alert_one_cluster():
    G = _simple_graph()
    alerts = [_make_alert("payment-svc", "2026-06-12T09:42:00Z")]
    clusters = correlate(alerts, G)
    assert len(clusters) == 1
    assert clusters[0]["alert_count"] == 1
    assert "payment-svc" in clusters[0]["services"]


def test_correlate_groups_related_services_in_time_window():
    G = _simple_graph()
    alerts = [
        _make_alert("payment-svc", "2026-06-12T09:42:00Z"),
        _make_alert("db-primary", "2026-06-12T09:42:30Z"),  # downstream of payment-svc
    ]
    clusters = correlate(alerts, G, gap_sec=120, max_hop=2)
    assert len(clusters) == 1, "Related services within time window → single cluster"
    assert clusters[0]["alert_count"] == 2


def test_correlate_splits_unrelated_services_far_apart():
    G = _simple_graph()
    # 10 minutes apart — beyond gap_sec=120
    alerts = [
        _make_alert("payment-svc", "2026-06-12T09:00:00Z"),
        _make_alert("db-primary", "2026-06-12T09:15:00Z"),
    ]
    clusters = correlate(alerts, G, gap_sec=120, max_hop=2)
    assert len(clusters) == 2, "Same services but >120s apart → separate clusters"


def test_correlate_cluster_id_is_stable():
    """Same set of alerts → same cluster_id (deterministic fingerprint)."""
    G = _simple_graph()
    alerts = [
        _make_alert("payment-svc", "2026-06-12T09:42:00Z"),
        _make_alert("db-primary", "2026-06-12T09:42:10Z"),
    ]
    c1 = correlate(alerts, G)
    c2 = correlate(alerts, G)
    assert c1[0]["cluster_id"] == c2[0]["cluster_id"]


def test_build_graph_from_json(tmp_path):
    import json
    data = {
        "services": [{"id": "a"}, {"id": "b"}],
        "edges": [{"src": "a", "dst": "b", "weight": 0.9}],
    }
    p = tmp_path / "svc.json"
    p.write_text(json.dumps(data))
    G = build_graph_from_json(str(p))
    assert G.has_edge("a", "b")
    assert G.number_of_nodes() == 2
