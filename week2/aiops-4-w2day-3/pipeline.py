"""
pipeline.py — Glue layer: chains correlate → rca → format response.

State is loaded once at module import and cached in memory.
Graph can be reloaded via reload_graph() (e.g. called by a background thread).
"""
import json
import logging
import threading
import time
from pathlib import Path

from correlate import build_graph_from_json, correlate
from rca import find_similar_incidents, run_rca

logger = logging.getLogger("aiops")

# ---------------------------------------------------------------------------
# State — loaded once, refreshable
# ---------------------------------------------------------------------------

_GRAPH_PATH = Path(__file__).parent / "dataset" / "services.json"
_HISTORY_PATH = Path(__file__).parent / "dataset" / "incidents_history.json"

_state_lock = threading.Lock()

_graph = None
_graph_loaded_at: str = ""
_graph_version: str = ""
_history: list[dict] = []


def _load_graph():
    global _graph, _graph_loaded_at, _graph_version
    g = build_graph_from_json(str(_GRAPH_PATH))
    import hashlib, datetime
    content = _GRAPH_PATH.read_bytes()
    version = "g-" + hashlib.md5(content).hexdigest()[:8].upper()
    loaded_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _state_lock:
        _graph = g
        _graph_version = version
        _graph_loaded_at = loaded_at
    logger.info(f"Graph loaded: {g.number_of_nodes()} nodes, {g.number_of_edges()} edges, version={version}")


def _load_history():
    global _history
    data = json.loads(_HISTORY_PATH.read_text())
    with _state_lock:
        _history = data.get("incidents", [])
    logger.info(f"History loaded: {len(_history)} past incidents")


def reload_graph():
    """Public API — reload graph from disk (called by background thread or test)."""
    _load_graph()


def get_graph_meta() -> dict:
    with _state_lock:
        return {
            "graph_version": _graph_version,
            "graph_loaded_at": _graph_loaded_at,
            "graph_source": str(_GRAPH_PATH),
            "graph_node_count": _graph.number_of_nodes() if _graph else 0,
            "graph_edge_count": _graph.number_of_edges() if _graph else 0,
        }


# ---------------------------------------------------------------------------
# Background graph refresh (every 5 minutes)
# ---------------------------------------------------------------------------

def _start_graph_refresh_thread(interval_sec: int = 300):
    def _worker():
        while True:
            time.sleep(interval_sec)
            try:
                _load_graph()
            except Exception as e:
                logger.error(f"Background graph reload failed: {e}")

    t = threading.Thread(target=_worker, daemon=True, name="graph-refresh")
    t.start()
    logger.info(f"Graph refresh thread started (interval={interval_sec}s)")


# ---------------------------------------------------------------------------
# Initial load
# ---------------------------------------------------------------------------
_load_graph()
_load_history()
_start_graph_refresh_thread(interval_sec=300)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_batch(alerts: list[dict]) -> dict:
    """
    Full 3-layer pipeline:
      L1: Correlate alerts into clusters
      L2: Graph-based RCA on primary cluster
      L3: LLM enrichment (optional, with fallback)

    Returns dict matching IncidentResponse schema.
    """
    with _state_lock:
        graph = _graph
        history = _history

    # ---- L1: Correlate ----
    clusters = correlate(alerts, graph, gap_sec=120, max_hop=2)

    if not clusters:
        return {
            "clusters": [],
            "root_cause": {
                "service": "unknown",
                "confidence": 0.0,
                "reasoning": "No alert clusters formed — alerts may be isolated or too sparse.",
            },
            "recommended_actions": ["Check individual service logs", "Verify alert thresholds"],
            "similar_incidents": [],
        }

    # Primary incident = largest cluster
    primary = max(clusters, key=lambda c: c["alert_count"])

    # ---- L2 + L3: RCA + LLM enrichment ----
    rca_result = run_rca(primary, alerts, graph, history)

    # Build similar_incidents list with summaries
    similar_ids = rca_result.get("similar_incidents", [])
    history_map = {inc["id"]: inc for inc in history}
    similar_out = []
    for inc_id in similar_ids[:3]:
        if inc_id in history_map:
            inc = history_map[inc_id]
            similar_out.append({
                "id": inc_id,
                "similarity": 0.75,
                "summary": inc.get("summary", ""),
            })
        else:
            similar_out.append({"id": inc_id, "similarity": 0.7, "summary": ""})

    return {
        "clusters": [
            {
                "cluster_id": c["cluster_id"],
                "alert_count": c["alert_count"],
                "services": c["services"],
                "time_range": c["time_range"],
            }
            for c in clusters
        ],
        "root_cause": {
            "service": rca_result.get("root_cause", "unknown"),
            "confidence": rca_result.get("confidence", 0.0),
            "reasoning": rca_result.get("reasoning", ""),
        },
        "recommended_actions": rca_result.get("actions", []),
        "similar_incidents": similar_out,
    }
