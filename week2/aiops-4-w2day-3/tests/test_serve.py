"""Integration tests for serve.py endpoints — uses FastAPI TestClient."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient
from serve import app

client = TestClient(app)

# ---------------------------------------------------------------------------
# Ops endpoints
# ---------------------------------------------------------------------------

def test_healthz_returns_ok():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_returns_ready():
    r = client.get("/readyz")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ready"
    assert data["checks"]["graph"] is True
    assert data["checks"]["history"] is True


def test_version_returns_expected_fields():
    r = client.get("/version")
    assert r.status_code == 200
    data = r.json()
    assert "app" in data
    assert "pipeline_config" in data
    assert "graph_node_count" in data


# ---------------------------------------------------------------------------
# Validation — 400/422 on bad input
# ---------------------------------------------------------------------------

def test_empty_alerts_returns_400():
    r = client.post("/incident", json={"alerts": []})
    assert r.status_code == 400
    assert "Empty alert list" in r.json()["detail"]


def test_missing_required_field_returns_422():
    # Missing 'ts', 'service', etc.
    r = client.post("/incident", json={"alerts": [{"id": "a-1"}]})
    assert r.status_code == 422


def test_invalid_json_body():
    r = client.post(
        "/incident",
        content="not-json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

VALID_PAYLOAD = {
    "alerts": [
        {
            "id": "a-1",
            "ts": "2026-06-12T09:42:01Z",
            "service": "payment-svc",
            "metric": "latency_p99_ms",
            "severity": "crit",
            "value": 1840,
            "threshold": 800,
        },
        {
            "id": "a-2",
            "ts": "2026-06-12T09:42:15Z",
            "service": "db-primary",
            "metric": "connections_used",
            "severity": "crit",
            "value": 950,
            "threshold": 800,
        },
    ]
}


def test_incident_happy_path_structure():
    """Endpoint returns correct structure — LLM mocked to avoid real calls."""
    with patch("rca.call_llm_rca") as mock_llm:
        mock_llm.return_value = {
            "root_cause": "payment-svc",
            "class": "connection_pool_exhaustion",
            "confidence": 0.84,
            "reasoning": "Payment service caused cascade to DB.",
            "actions": ["Scale payment-svc", "Increase DB pool", "Add circuit breaker"],
            "similar_incidents": ["INC-2025-11-08"],
        }
        r = client.post("/incident", json=VALID_PAYLOAD)

    assert r.status_code == 200
    data = r.json()
    assert "clusters" in data
    assert "root_cause" in data
    assert "recommended_actions" in data
    assert "similar_incidents" in data


def test_incident_root_cause_has_required_fields():
    with patch("rca.call_llm_rca") as mock_llm:
        mock_llm.return_value = {
            "root_cause": "payment-svc",
            "confidence": 0.84,
            "reasoning": "mocked",
            "actions": [],
            "similar_incidents": [],
        }
        r = client.post("/incident", json=VALID_PAYLOAD)

    assert r.status_code == 200
    rc = r.json()["root_cause"]
    assert "service" in rc
    assert "confidence" in rc
    assert "reasoning" in rc


def test_incident_clusters_non_empty():
    with patch("rca.call_llm_rca") as mock_llm:
        mock_llm.return_value = {
            "root_cause": "payment-svc",
            "confidence": 0.84,
            "reasoning": "mocked",
            "actions": [],
            "similar_incidents": [],
        }
        r = client.post("/incident", json=VALID_PAYLOAD)

    assert r.status_code == 200
    assert len(r.json()["clusters"]) >= 1


def test_incident_single_alert():
    payload = {
        "alerts": [{
            "id": "a-solo",
            "ts": "2026-06-12T09:42:01Z",
            "service": "payment-svc",
            "metric": "error_rate",
            "severity": "warn",
            "value": 5.2,
            "threshold": 1.0,
        }]
    }
    with patch("rca.call_llm_rca") as mock_llm:
        mock_llm.return_value = {
            "root_cause": "payment-svc",
            "confidence": 0.7,
            "reasoning": "mocked",
            "actions": ["Check logs"],
            "similar_incidents": [],
        }
        r = client.post("/incident", json=payload)
    assert r.status_code == 200


def test_x_response_time_header_present():
    r = client.get("/healthz")
    assert "X-Response-Time-Ms" in r.headers


def test_llm_failure_falls_back_gracefully():
    """If LLM fails, pipeline should still return 200 via graph-only fallback."""
    with patch("rca.call_llm_rca", side_effect=Exception("LLM provider down")):
        r = client.post("/incident", json=VALID_PAYLOAD)
    assert r.status_code == 200
    data = r.json()
    assert data["root_cause"]["service"] != ""
