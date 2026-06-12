"""
pipeline.py — Glue layer: correlate → RCA → format cho IncidentResponse

Load service graph + incident history 1 lần ở module level (cache),
expose process_batch(alerts) cho serve.py.
"""
import logging
from pathlib import Path

from correlate import build_graph_from_file, correlate
from rca import run_rca
import json

logger = logging.getLogger('aiops')

DATASET_DIR = Path(__file__).parent / 'dataset'

# ---------------------------------------------------------------------------
# Module-level cache — load 1 lần khi import
# ---------------------------------------------------------------------------
_graph, _svc_data = build_graph_from_file(str(DATASET_DIR / 'services.json'))
_history = json.loads((DATASET_DIR / 'incidents_history.json').read_text(encoding='utf-8'))

_graph_version = 'g-local-001'
_graph_source = 'manual'


def get_graph_meta() -> dict:
    """Metadata cho /version endpoint."""
    return {
        'graph_version':    _graph_version,
        'graph_loaded_at':  None,
        'graph_source':     _graph_source,
        'graph_node_count': _graph.number_of_nodes(),
        'graph_edge_count': _graph.number_of_edges(),
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def process_batch(alerts: list[dict], gap_sec: int = 120, max_hop: int = 2) -> dict:
    """
    Flow:
      1. correlate(alerts, GRAPH, gap_sec, max_hop) → list[cluster]
      2. Nếu rỗng → return early với root_cause unknown
      3. Pick cluster lớn nhất (alert_count) làm primary incident
      4. run_rca(primary, GRAPH, HISTORY) → dict (root_cause, confidence, actions, reasoning, similar_incidents)
      5. Pack lại thành dict matching IncidentResponse schema
    """
    clusters = correlate(alerts, _graph, gap_sec=gap_sec, max_hop=max_hop)

    if not clusters:
        return {
            'clusters': [],
            'root_cause': {
                'service':    'unknown',
                'confidence': 0.0,
                'reasoning':  'No clusters formed from input alerts.',
            },
            'recommended_actions': ['Investigate manually'],
            'similar_incidents': [],
        }

    primary = max(clusters, key=lambda c: c['alert_count'])

    rca_result = run_rca(primary, _graph, _history)

    cluster_summaries = [
        {
            'cluster_id':  c['cluster_id'],
            'alert_count': c['alert_count'],
            'services':    c['services'],
            'time_range':  c['time_range'],
        }
        for c in clusters
    ]

    similar_out = []
    for inc_id in rca_result.get('similar_incidents', []):
        inc = next((i for i in _history['incidents'] if i['id'] == inc_id), None)
        if inc:
            similar_out.append({
                'id':         inc['id'],
                'similarity': 1.0,
                'summary':    inc['summary'],
            })

    return {
        'clusters': cluster_summaries,
        'root_cause': {
            'service':    rca_result.get('root_cause', primary['services'][0]),
            'confidence': rca_result.get('confidence', 0.0),
            'reasoning':  rca_result.get('reasoning', ''),
        },
        'recommended_actions': rca_result.get('actions', []),
        'similar_incidents': similar_out,
    }
