"""Layer 2 — Retrieval + Outcome-Weighted Voting.

Finds top-k historical incidents most similar to the current incident,
then votes on a candidate action list weighted by outcome success.

Similarity function
-------------------
Weighted combination of 3 signals:
  1. log_overlap      : Jaccard of log template *tokens* vs history log_signatures
  2. trace_overlap    : Jaccard of (from,to) error edges vs history trace_signatures
  3. service_overlap  : Jaccard of affected_services

OOD detection
-------------
If max_similarity < OOD_THRESHOLD → no close match → escalate.

Outcome weighting
-----------------
  weight(neighbor) = similarity(n) * outcome_weight(n.outcome)
outcome_weight: success=1.0, partial=0.5, failed=0.1
"""

from __future__ import annotations
import re
from collections import defaultdict
from typing import Any

OOD_THRESHOLD = 0.22   # below this → novel incident
TOP_K = 5

OUTCOME_WEIGHT = {
    'success': 1.0,
    'partial': 0.5,
    'failed': 0.1,
}


# ---------------------------------------------------------------------------
# History parsing helpers (matches optional-helpers.py)
# ---------------------------------------------------------------------------

def _parse_history_action(s: str) -> dict:
    parts = s.split(':')
    if not parts:
        return {'name': 'page_oncall', 'params': []}
    return {'name': parts[0], 'params': parts[1:]}


def _drain_tokens(s: str) -> set[str]:
    """Tokenise a log signature/template into a bag-of-words set for fuzzy match."""
    s = re.sub(r'[^a-zA-Z0-9 _]', ' ', s)
    return {t.lower() for t in s.split() if len(t) > 2}


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def similarity(query_vec: dict, history_entry: dict) -> float:
    """
    Weighted similarity between a query incident_vector and a history entry.
    Returns float in [0, 1].
    """
    # --- 1. Log similarity: token overlap between query templates and history log_signatures ---
    query_log_tokens: set[str] = set()
    for tmpl in query_vec.get('log_template_set', set()):
        query_log_tokens |= _drain_tokens(tmpl)

    hist_log_tokens: set[str] = set()
    for sig in history_entry.get('log_signatures', []):
        hist_log_tokens |= _drain_tokens(sig)

    log_sim = _jaccard(query_log_tokens, hist_log_tokens)

    # --- 2. Trace similarity: edge-level error signal overlap ---
    query_trace_feats = query_vec.get('trace_features', {})
    # Extract high-error edges from query (error_rate > 0.1)
    query_edges = {
        (v['from'], v['to'])
        for v in query_trace_feats.values()
        if v.get('error_rate', 0) > 0.1
    }
    # Also collect just the "to" services (destination = problem service)
    query_to_svcs = {v['to'] for v in query_trace_feats.values() if v.get('error_rate', 0) > 0.1}

    hist_edges = {
        (t['from'], t['to'])
        for t in history_entry.get('trace_signatures', [])
    }
    hist_to_svcs = {t['to'] for t in history_entry.get('trace_signatures', [])}

    trace_edge_sim = _jaccard(query_edges, hist_edges)
    trace_svc_sim  = _jaccard(query_to_svcs, hist_to_svcs)
    trace_sim = 0.5 * trace_edge_sim + 0.5 * trace_svc_sim

    # --- 3. Affected service overlap ---
    query_svcs = set(query_vec.get('affected_services', []))
    hist_svcs = set(history_entry.get('affected_services', []))
    svc_sim = _jaccard(query_svcs, hist_svcs)

    # Weighted combination — log + trace are stronger signals
    score = 0.45 * log_sim + 0.35 * trace_sim + 0.20 * svc_sim
    return round(score, 4)


# ---------------------------------------------------------------------------
# kNN + outcome-weighted voting
# ---------------------------------------------------------------------------

def retrieve_and_vote(
    query_vec: dict,
    history: list[dict],
    top_k: int = TOP_K,
) -> dict:
    """
    1. Score all history entries by similarity to query.
    2. Take top_k.
    3. Vote on candidate actions weighted by similarity * outcome_weight.
    4. Return voting results + OOD flag.
    """
    scored = []
    for entry in history:
        sim = similarity(query_vec, entry)
        scored.append((sim, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]

    max_sim = top[0][0] if top else 0.0
    is_ood = max_sim < OOD_THRESHOLD

    # --- Weighted voting ---
    action_votes: dict[str, float] = defaultdict(float)
    action_success_count: dict[str, int] = defaultdict(int)
    action_total_count: dict[str, int] = defaultdict(int)

    for sim, entry in top:
        ow = OUTCOME_WEIGHT.get(entry.get('outcome', 'failed'), 0.1)
        weight = (sim ** 2) * ow  # squared sim amplifies dominant neighbors

        for raw_action in entry.get('actions_taken', []):
            parsed = _parse_history_action(raw_action)
            action_name = parsed['name']
            action_params = parsed['params']

            action_votes[action_name] += weight
            action_total_count[action_name] += 1
            if entry.get('outcome') == 'success':
                action_success_count[action_name] += 1

    # Build candidate list sorted by vote weight
    candidates = []
    for action_name, vote_weight in sorted(action_votes.items(), key=lambda x: x[1], reverse=True):
        total = action_total_count[action_name]
        successes = action_success_count[action_name]
        p_success = successes / total if total > 0 else 0.0
        candidates.append({
            'action': action_name,
            'vote_weight': round(vote_weight, 4),
            'p_success': round(p_success, 3),
            'appearances': total,
        })

    # Build neighbor summaries for audit
    neighbor_summaries = []
    for sim, entry in top:
        neighbor_summaries.append({
            'id': entry.get('id'),
            'similarity': sim,
            'root_cause_class': entry.get('root_cause_class'),
            'outcome': entry.get('outcome'),
            'actions_taken': entry.get('actions_taken', []),
        })

    return {
        'candidates': candidates,
        'top_3_neighbors': neighbor_summaries[:3],
        'max_similarity': max_sim,
        'is_ood': is_ood,
        'ood_threshold': OOD_THRESHOLD,
    }
