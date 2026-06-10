"""
serve.py — AIOps Incident Pipeline HTTP Service

Run: uvicorn serve:app --host 0.0.0.0 --port 8000 --workers 1 --reload

Env vars:
  GROQ_API_KEY     — Groq API key (bắt buộc nếu AIOPS_USE_LLM=true)
  AIOPS_USE_LLM    — 'true' (default) | 'false' để bypass LLM
  AIOPS_LLM_MODEL  — default 'llama-3.3-70b-versatile'
"""
import json
import logging
import os
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from dotenv import load_dotenv
load_dotenv()
# ---------------------------------------------------------------------------
# Structured JSON logger
# ---------------------------------------------------------------------------
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            'ts':     self.formatTime(record, '%Y-%m-%dT%H:%M:%S'),
            'level':  record.levelname,
            'msg':    record.getMessage(),
            'logger': record.name,
        }
        if hasattr(record, 'extra'):
            obj.update(record.extra)
        return json.dumps(obj, ensure_ascii=False)


_handler = logging.StreamHandler()
_handler.setFormatter(_JsonFormatter())
_logger = logging.getLogger('aiops')
_logger.addHandler(_handler)
_logger.setLevel(logging.INFO)
_logger.propagate = False

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
APP_VERSION = '1.0.0'

app = FastAPI(
    title='AIOps Incident Pipeline',
    version=APP_VERSION,
    description=(
        'POST a batch of alerts → 3-layer pipeline (correlate → graph RCA → Groq LLM) '
        '→ incident report với root cause + recommended actions.'
    ),
)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class Alert(BaseModel):
    id:        str
    ts:        str
    service:   str
    metric:    str
    severity:  str
    value:     float
    threshold: float = 0.0
    labels:    Optional[dict] = Field(default_factory=dict)

class IncidentRequest(BaseModel):
    alerts: list[Alert]

class Cluster(BaseModel):
    cluster_id:  str
    alert_count: int
    services:    list[str]
    time_range:  list[str]

class RootCause(BaseModel):
    service:    str
    confidence: float
    reasoning:  str

class SimilarIncident(BaseModel):
    id:         str
    similarity: float
    summary:    str

class IncidentResponse(BaseModel):
    clusters:             list[Cluster]
    root_cause:           RootCause
    recommended_actions:  list[str]
    similar_incidents:    list[SimilarIncident]

# ---------------------------------------------------------------------------
# Latency middleware
# ---------------------------------------------------------------------------
@app.middleware('http')
async def add_timing(request: Request, call_next):
    start    = time.perf_counter()
    response = await call_next(request)
    ms       = (time.perf_counter() - start) * 1000
    response.headers['X-Response-Time-Ms'] = f'{ms:.1f}'
    _logger.info(f'{request.method} {request.url.path} → {response.status_code} ({ms:.0f}ms)')
    return response

# ---------------------------------------------------------------------------
# Ops endpoints
# ---------------------------------------------------------------------------
@app.get('/healthz', tags=['ops'])
def healthz() -> dict:
    """Liveness probe — process còn sống không."""
    return {'status': 'ok'}


@app.get('/readyz', tags=['ops'])
def readyz() -> dict:
    """
    Readiness probe — graph + history đã load chưa.
    Trả 503 nếu chưa sẵn sàng (dùng khi rolling deploy).
    """
    from pipeline import _graph, _history
    checks = {
        'graph':   _graph is not None and _graph.number_of_nodes() > 0,
        'history': _history is not None and len(_history.get('incidents', [])) > 0,
    }
    if not all(checks.values()):
        raise HTTPException(status_code=503, detail={'status': 'not ready', 'checks': checks})
    return {'status': 'ready', 'checks': checks}


@app.get('/version', tags=['ops'])
def version() -> dict:
    """App version + pipeline config + graph metadata."""
    from pipeline import get_graph_meta
    return {
        'app': APP_VERSION,
        'pipeline_config': {
            'correlate_gap_sec': 120,
            'correlate_max_hop': 2,
            'rca_method':        'graph+llm' if os.environ.get('AIOPS_USE_LLM', 'true').lower() == 'true' else 'graph-only',
            'llm_provider':      'groq',
            'llm_model':         os.environ.get('AIOPS_LLM_MODEL', 'llama-3.3-70b-versatile'),
        },
        **get_graph_meta(),
    }

# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------
@app.post('/incident', response_model=IncidentResponse, tags=['pipeline'])
def post_incident(req: IncidentRequest) -> IncidentResponse:
    """
    Nhận batch alerts → chạy 3-layer pipeline:
      1. correlate()    — session window + topology grouping (w2/d1)
      2. rca_combined() — graph PageRank + temporal scoring (w2/d2)
      3. Groq LLM       — classify + reasoning + actions (w2/d2 bonus)

    Fallback tự động sang graph+retrieval nếu Groq API fail.
    """
    if not req.alerts:
        raise HTTPException(status_code=400, detail='Empty alert list — cần ít nhất 1 alert.')

    _logger.info(
        'Received incident request',
        extra={'extra': {
            'alert_count': len(req.alerts),
            'services':    list({a.service for a in req.alerts}),
        }},
    )

    from pipeline import process_batch
    alerts_dict = [a.model_dump() for a in req.alerts]

    try:
        result = process_batch(alerts_dict)
    except Exception as e:
        _logger.error(f'Pipeline error: {e}', exc_info=True)
        raise HTTPException(status_code=500, detail=f'Pipeline error: {str(e)}')

    _logger.info(
        'Incident processed',
        extra={'extra': {
            'cluster_count': len(result['clusters']),
            'root_cause':    result['root_cause']['service'],
            'confidence':    result['root_cause']['confidence'],
        }},
    )

    return IncidentResponse(**result)
