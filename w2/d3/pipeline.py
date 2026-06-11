"""
pipeline.py — Glue layer: correlate → rca → format response.

Dùng đúng dataset format từ w2/d1 và w2/d2 của bạn:
  - services.json có 'services', 'stores', 'edges'
  - incidents_history.json có 'incidents' list
"""
import json
import logging
import threading
import time
import datetime
import hashlib
from pathlib import Path

from correlate import build_graph, correlate
from rca import run_rca

logger = logging.getLogger('aiops')

# ---------------------------------------------------------------------------
# Paths — dataset nằm cùng folder với serve.py
# ---------------------------------------------------------------------------
_BASE        = Path(__file__).parent
_SVC_PATH    = _BASE / 'dataset' / 'services.json'
_HIST_PATH   = _BASE / 'dataset' / 'incidents_history.json'

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_lock           = threading.Lock()
_graph          = None
_svc_data       = None
_history        = None
_graph_version  = ''
_graph_loaded_at = ''


def _load_all():
    global _graph, _svc_data, _history, _graph_version, _graph_loaded_at

    svc_raw  = json.loads(_SVC_PATH.read_text(encoding='utf-8'))
    hist_raw = json.loads(_HIST_PATH.read_text(encoding='utf-8'))
    g        = build_graph(svc_raw)
    version  = 'g-' + hashlib.md5(_SVC_PATH.read_bytes()).hexdigest()[:8].upper()
    loaded_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with _lock:
        _graph           = g
        _svc_data        = svc_raw
        _history         = hist_raw
        _graph_version   = version
        _graph_loaded_at = loaded_at

    logger.info(f'Graph loaded: {g.number_of_nodes()} nodes, {g.number_of_edges()} edges, version={version}')
    logger.info(f'History loaded: {len(hist_raw["incidents"])} past incidents')


def reload_graph():
    _load_all()


def get_graph_meta() -> dict:
    with _lock:
        return {
            'graph_version':    _graph_version,
            'graph_loaded_at':  _graph_loaded_at,
            'graph_source':     str(_SVC_PATH),
            'graph_node_count': _graph.number_of_nodes() if _graph else 0,
            'graph_edge_count': _graph.number_of_edges() if _graph else 0,
        }


# ---------------------------------------------------------------------------
# Background refresh mỗi 5 phút
# ---------------------------------------------------------------------------
def _start_refresh(interval: int = 300):
    def _worker():
        while True:
            time.sleep(interval)
            try:
                _load_all()
            except Exception as e:
                logger.error(f'Background graph reload failed: {e}')
    t = threading.Thread(target=_worker, daemon=True, name='graph-refresh')
    t.start()


# Initial load
_load_all()
_start_refresh(300)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def process_batch(alerts: list[dict]) -> dict:
    """
    3-layer pipeline:
      L1: correlate()    — gom alerts thành clusters (từ d1)
      L2: rca_combined() — graph scoring (từ d2)
      L3: Groq LLM       — enrichment + classify (từ d2, với fallback)
    """
    with _lock:
        graph   = _graph
        history = _history

    # ── L1: Correlate ──────────────────────────────────────────────────────
    clusters = correlate(alerts, graph, gap_sec=120, max_hop=2)

    if not clusters:
        return {
            'clusters': [],
            'root_cause': {
                'service':    'unknown',
                'confidence': 0.0,
                'reasoning':  'No clusters formed — alerts may be too sparse or unrelated.',
            },
            'recommended_actions': ['Check individual service logs', 'Verify alert thresholds'],
            'similar_incidents':   [],
        }

    # Primary cluster = lớn nhất
    primary = max(clusters, key=lambda c: c['alert_count'])

    # ── L2 + L3: RCA ───────────────────────────────────────────────────────
    rca_result = run_rca(primary, graph, history)

    # Build similar_incidents output
    hist_map     = {inc['id']: inc for inc in history['incidents']}
    similar_out  = []
    for inc_id in rca_result.get('similar_incidents', [])[:3]:
        if inc_id in hist_map:
            inc = hist_map[inc_id]
            similar_out.append({
                'id':         inc_id,
                'similarity': 0.75,
                'summary':    inc.get('summary', ''),
            })

    return {
        'clusters': [
            {
                'cluster_id':  c['cluster_id'],
                'alert_count': c['alert_count'],
                'services':    c['services'],
                'time_range':  c['time_range'],
            }
            for c in clusters
        ],
        'root_cause': {
            'service':    rca_result.get('root_cause', 'unknown'),
            'confidence': rca_result.get('confidence', 0.0),
            'reasoning':  rca_result.get('reasoning', ''),
        },
        'recommended_actions': rca_result.get('actions', []),
        'similar_incidents':   similar_out,
    }
