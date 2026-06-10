"""
serve.py — AIOps Incident Pipeline HTTP Service

Run: uvicorn serve:app --host 0.0.0.0 --port 8000 --reload
"""
import json
import logging
import os
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Structured JSON logger
# ---------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "msg": record.getMessage(),
            "logger": record.name,
        }
        if hasattr(record, "extra"):
            obj.update(record.extra)
        return json.dumps(obj)


def _setup_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger("aiops")
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False


_setup_logging()
logger = logging.getLogger("aiops")

# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------

APP_VERSION = "1.0.0"

app = FastAPI(
    title="AIOps Incident Pipeline",
    version=APP_VERSION,
    description="Correlate alerts → RCA → suggest action. POST a batch of alerts, receive an incident report.",
)

# ---------------------------------------------------------------------------
# Input / Output schemas (Pydantic v2)
# ---------------------------------------------------------------------------

class Alert(BaseModel):
    id: str
    ts: str
    service: str
    metric: str
    severity: str
    value: float
    threshold: float = 0.0
    labels: Optional[dict] = Field(default_factory=dict)


class IncidentRequest(BaseModel):
    alerts: list[Alert]


class Cluster(BaseModel):
    cluster_id: str
    alert_count: int
    services: list[str]
    time_range: list[str]


class RootCause(BaseModel):
    service: str
    confidence: float
    reasoning: str


class SimilarIncident(BaseModel):
    id: str
    similarity: float
    summary: str


class IncidentResponse(BaseModel):
    clusters: list[Cluster]
    root_cause: RootCause
    recommended_actions: list[str]
    similar_incidents: list[SimilarIncident]


# ---------------------------------------------------------------------------
# Latency middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def add_timing(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"
    logger.info(
        f"{request.method} {request.url.path} → {response.status_code} ({duration_ms:.0f}ms)"
    )
    return response


# ---------------------------------------------------------------------------
# Health + Readiness endpoints
# ---------------------------------------------------------------------------

@app.get("/healthz", tags=["ops"])
def healthz() -> dict:
    """Liveness probe — is the process alive?"""
    return {"status": "ok"}


@app.get("/readyz", tags=["ops"])
def readyz() -> dict:
    """
    Readiness probe — are all dependencies loaded and ready?
    Returns 503 if graph or history not loaded.
    Used by load balancers during rolling deploys.
    """
    from pipeline import _graph, _history

    checks = {
        "graph": _graph is not None and _graph.number_of_nodes() > 0,
        "history": len(_history) > 0,
    }

    if not all(checks.values()):
        raise HTTPException(status_code=503, detail={"status": "not ready", "checks": checks})

    return {"status": "ready", "checks": checks}


# ---------------------------------------------------------------------------
# Version endpoint
# ---------------------------------------------------------------------------

@app.get("/version", tags=["ops"])
def version() -> dict:
    """Return app + pipeline config + graph metadata."""
    from pipeline import get_graph_meta
    meta = get_graph_meta()
    return {
        "app": APP_VERSION,
        "pipeline_config": {
            "correlate_gap_sec": 120,
            "correlate_max_hop": 2,
            "rca_method": "graph+llm" if os.environ.get("AIOPS_USE_LLM", "true").lower() == "true" else "graph-only",
            "llm_model": os.environ.get("AIOPS_LLM_MODEL", "gpt-4o-mini"),
        },
        **meta,
    }


# ---------------------------------------------------------------------------
# Main incident endpoint
# ---------------------------------------------------------------------------

@app.post("/incident", response_model=IncidentResponse, tags=["pipeline"])
def post_incident(req: IncidentRequest) -> IncidentResponse:
    """
    Process a batch of alerts through the 3-layer pipeline:
      1. Correlation  — group related alerts into clusters
      2. Graph RCA    — identify root cause via PageRank on dependency graph
      3. LLM enrichment — explain, classify, suggest actions (with fallback)

    Returns an incident report with clusters, root cause, and recommended actions.
    """
    if not req.alerts:
        raise HTTPException(status_code=400, detail="Empty alert list — provide at least 1 alert.")

    logger.info(
        "Received incident request",
        extra={"extra": {"alert_count": len(req.alerts), "services": list({a.service for a in req.alerts})}},
    )

    from pipeline import process_batch

    alerts_dict = [a.model_dump() for a in req.alerts]

    try:
        result = process_batch(alerts_dict)
    except Exception as e:
        logger.error(f"Pipeline error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Pipeline error: {str(e)}")

    logger.info(
        "Incident processed",
        extra={"extra": {
            "cluster_count": len(result["clusters"]),
            "root_cause": result["root_cause"]["service"],
            "confidence": result["root_cause"]["confidence"],
        }},
    )

    return IncidentResponse(**result)
