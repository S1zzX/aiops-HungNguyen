"""Layer 1 — Feature Extraction.

Converts a raw incident JSON into a compact incident_vector that
Layer 2 can compare across incidents.

Feature groups
--------------
1. log_template_counts  : dict[template_str -> count]  (log clustering via Drain-lite)
2. trace_features       : dict[edge_key -> {error_rate, p99_deviation}]
3. affected_services    : set[str]
4. alert_service        : str
5. severity             : str
"""

from __future__ import annotations
import re
from collections import defaultdict
from typing import Any


# ---------------------------------------------------------------------------
# Log clustering (Drain-lite: strip tokens, keep skeleton)
# ---------------------------------------------------------------------------

_NUMBER  = re.compile(r'\b\d+(\.\d+)?(ms|MB|GB|s|%)?\b')
_HEX     = re.compile(r'\b[0-9a-fA-F]{6,}\b')
_UUID    = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
_PATH    = re.compile(r'(/[\w./\-]+)')
_VERSION = re.compile(r'\bv\d+\.\d+[\.\d]*\b')
_IP      = re.compile(r'\b\d{1,3}(\.\d{1,3}){3}\b')


def _drain_template(msg: str) -> str:
    """Reduce a log message to a stable template by stripping variable tokens."""
    s = _UUID.sub('<UUID>', msg)
    s = _IP.sub('<IP>', s)
    s = _VERSION.sub('<VER>', s)
    s = _HEX.sub('<HEX>', s)
    s = _PATH.sub('<PATH>', s)
    s = _NUMBER.sub('<NUM>', s)
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def extract_log_templates(logs: list[dict]) -> dict[str, int]:
    """Return template -> count mapping from raw log list."""
    counts: dict[str, int] = defaultdict(int)
    for entry in logs:
        tmpl = _drain_template(entry.get('msg', ''))
        counts[tmpl] += 1
    return dict(counts)


# ---------------------------------------------------------------------------
# Trace feature extraction
# ---------------------------------------------------------------------------

def extract_trace_features(traces: list[dict], metrics_window: dict) -> dict[str, dict]:
    """
    Per (from, to) edge:
      - error_rate: error_count / count
      - p99_deviation: p99_ms / p50_ms  (proxy for deviation from baseline)
    Also compute a p99_deviation_ratio using metric baselines if available.
    """
    edge_feats: dict[str, dict] = {}
    for t in traces:
        key = f"{t['from']}->{t['to']}"
        count = t.get('count', 1) or 1
        err_rate = t.get('error_count', 0) / count
        p50 = t.get('p50_ms', 1) or 1
        p99 = t.get('p99_ms', p50)
        edge_feats[key] = {
            'error_rate': round(err_rate, 4),
            'p99_deviation_ratio': round(p99 / p50, 2),
            'p99_ms': p99,
            'from': t['from'],
            'to': t['to'],
        }
    return edge_feats


# ---------------------------------------------------------------------------
# Affected services detection
# ---------------------------------------------------------------------------

def extract_affected_services(incident: dict) -> list[str]:
    """Union services from: alert, error-rate traces, error logs, metric spikes."""
    services = set()

    # From alert
    alert_svc = incident.get('trigger_alert', {}).get('service')
    if alert_svc:
        services.add(alert_svc)

    # From traces with high error rate (> 0.1)
    for t in incident.get('traces', []):
        count = t.get('count', 1) or 1
        err_rate = t.get('error_count', 0) / count
        if err_rate > 0.1:
            services.add(t['from'])
            services.add(t['to'])

    # From error logs
    for log in incident.get('logs', []):
        if log.get('level') in ('ERROR', 'CRITICAL'):
            svc = log.get('svc')
            if svc:
                services.add(svc)

    return sorted(services)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def extract_features(incident: dict) -> dict[str, Any]:
    """Convert raw incident JSON into a comparable incident_vector."""
    logs = incident.get('logs', [])
    traces = incident.get('traces', [])
    metrics = incident.get('metrics_window', {})

    log_templates = extract_log_templates(logs)
    trace_feats = extract_trace_features(traces, metrics)
    affected = extract_affected_services(incident)

    # Compute top error trace edges (sorted by error_rate desc)
    sorted_edges = sorted(
        trace_feats.items(),
        key=lambda x: (x[1]['error_rate'], x[1]['p99_deviation_ratio']),
        reverse=True
    )
    top_error_edge = sorted_edges[0] if sorted_edges else None

    return {
        'incident_id': incident.get('incident_id', ''),
        'alert_service': incident.get('trigger_alert', {}).get('service', ''),
        'severity': incident.get('trigger_alert', {}).get('severity', 'unknown'),
        'log_templates': log_templates,
        'trace_features': trace_feats,
        'affected_services': affected,
        'top_error_edge': top_error_edge,
        # Convenience flags for Layer 2
        'log_template_set': set(log_templates.keys()),
        'top_error_trace_service': top_error_edge[1]['to'] if top_error_edge else None,
    }
