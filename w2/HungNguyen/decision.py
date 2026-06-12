"""Layer 3 — Action Selection via Cost-Aware Utility + Blast-Radius Gate.

Decision procedure
------------------
1. If is_ood → page_oncall immediately (novel incident).
2. Compute expected value (EV) for each non-escalation candidate:
     EV(a) = p_success(a) * benefit - (1 - p_success(a)) * blast_penalty(a)
   where:
     benefit         = 1.0 (normalised)
     blast_penalty   = blast_radius_services / 4.0  (max=4 in catalog → 0..1)
     cost_penalty    = cost_min / 15.0              (max=15 for network_policy_revert)
   Final score = EV(a) - 0.1 * cost_penalty

3. Blast-radius gate: if best candidate has blast_radius_services >= BLAST_GATE
   AND confidence < CONFIDENCE_GATE → do NOT auto-act, page_oncall.

4. page_oncall has EV = 0 and is only chosen when nothing else clears the gate
   OR when is_ood. This prevents naive "zero cost" selection.

5. Confidence = p_success * min(1, max_sim / 0.4)
   (scales down confidence when similarity is weak but not OOD)

Parameters
----------
BLAST_GATE       : blast_radius_services threshold that triggers safety check
CONFIDENCE_GATE  : min confidence required to act on a high-blast-radius action
MIN_CONFIDENCE   : below this, always escalate regardless of blast radius
"""

from __future__ import annotations

BLAST_GATE       = 3    # blast_radius >= 3 triggers safety check
CONFIDENCE_GATE  = 0.50 # need >= this confidence to act when blast is high
MIN_CONFIDENCE   = 0.20 # below this: always escalate


def _action_meta(action_name: str, catalog: list[dict]) -> dict:
    for a in catalog:
        if a['name'] == action_name:
            return a
    return {}


def _infer_params(action_name: str, query_vec: dict, history_candidates: list[dict]) -> dict:
    """
    Infer action parameters from the top affected service.
    This is a best-effort heuristic — on-call will verify before executing.
    """
    top_svc = query_vec.get('top_error_trace_service') or query_vec.get('alert_service', '')
    affected = query_vec.get('affected_services', [])

    params: dict = {}

    if action_name == 'rollback_service':
        svc = top_svc or (affected[0] if affected else 'unknown-svc')
        params = {'service': svc, 'target_version': 'previous'}

    elif action_name == 'increase_pool_size':
        svc = top_svc or (affected[0] if affected else 'unknown-svc')
        params = {'service': svc, 'from_value': 50, 'to_value': 100}

    elif action_name == 'restart_pod':
        svc = top_svc or (affected[0] if affected else 'unknown-svc')
        params = {'service': svc, 'pod_selector': 'app=' + svc}

    elif action_name == 'dns_config_rollback':
        params = {'configmap_name': 'dns-config', 'target_revision': 'previous'}

    elif action_name == 'network_policy_revert':
        params = {'policy_name': 'default-network-policy'}

    elif action_name == 'page_oncall':
        params = {'team': 'platform-team'}

    return params


def select_action(
    retrieval_result: dict,
    actions_catalog: list[dict],
    query_vec: dict,
) -> dict:
    """
    Choose the best action given candidates, catalog metadata, and query context.
    Returns audit-ready decision dict.
    """
    is_ood         = retrieval_result.get('is_ood', False)
    max_sim        = retrieval_result.get('max_similarity', 0.0)
    candidates     = retrieval_result.get('candidates', [])
    top_neighbors  = retrieval_result.get('top_3_neighbors', [])

    # ── OOD: escalate immediately ────────────────────────────────────────────
    if is_ood:
        return _build_decision(
            action='page_oncall',
            params={'team': 'platform-team'},
            confidence=0.0,
            reason='OOD: max_similarity={:.3f} < threshold={:.3f}'.format(
                max_sim, retrieval_result.get('ood_threshold', 0.15)
            ),
            ev=0.0,
            retrieval=retrieval_result,
            query_vec=query_vec,
        )

    # ── Filter out page_oncall from auto-candidates; treat separately ────────
    auto_candidates = [c for c in candidates if c['action'] != 'page_oncall']

    if not auto_candidates:
        # No evidence for anything → escalate
        return _build_decision(
            action='page_oncall',
            params={'team': 'platform-team'},
            confidence=0.1,
            reason='No viable auto-action candidates from retrieval',
            ev=0.0,
            retrieval=retrieval_result,
            query_vec=query_vec,
        )

    # ── Compute EV for each candidate ───────────────────────────────────────
    # Normalize vote weights to get relative confidence
    total_vote = sum(c['vote_weight'] for c in auto_candidates) or 1.0

    ev_table = []
    for cand in auto_candidates:
        meta = _action_meta(cand['action'], actions_catalog)
        blast   = meta.get('blast_radius_services', 1)
        cost    = meta.get('cost_min', 1)
        p_suc   = cand['p_success']
        vote_frac = cand['vote_weight'] / total_vote  # normalized vote share

        # Confidence = blend of historical p_success and vote share from retrieval
        # vote_frac captures "how much this action dominated the neighbors"
        # p_suc captures "how often this action worked historically"
        confidence = 0.6 * vote_frac + 0.4 * p_suc
        # Scale by similarity quality
        confidence = confidence * min(1.0, max_sim / 0.40)

        blast_penalty = blast / 4.0
        cost_penalty  = cost / 15.0
        # EV uses vote-based p_success proxy (vote_frac) weighted with outcome p_success
        ev_p = 0.6 * vote_frac + 0.4 * p_suc
        ev = ev_p * 1.0 - (1 - ev_p) * blast_penalty - 0.10 * cost_penalty

        ev_table.append({
            'action':     cand['action'],
            'p_success':  p_suc,
            'vote_weight': cand['vote_weight'],
            'vote_frac':  round(vote_frac, 3),
            'confidence': round(confidence, 3),
            'ev':         round(ev, 4),
            'blast':      blast,
            'cost':       cost,
            'blast_penalty': round(blast_penalty, 3),
            'cost_penalty':  round(cost_penalty, 3),
        })

    ev_table.sort(key=lambda x: x['ev'], reverse=True)
    best = ev_table[0]

    # ── Blast-radius gate ───────────────────────────────────────────────────
    blast_gate_triggered = (
        best['blast'] >= BLAST_GATE and best['confidence'] < CONFIDENCE_GATE
    )
    min_confidence_gate = best['confidence'] < MIN_CONFIDENCE

    if blast_gate_triggered or min_confidence_gate:
        reason = (
            f"Blast gate: blast_radius={best['blast']} >= {BLAST_GATE} "
            f"and confidence={best['confidence']:.2f} < {CONFIDENCE_GATE}"
            if blast_gate_triggered
            else f"Low confidence={best['confidence']:.2f} < MIN={MIN_CONFIDENCE}"
        )
        return _build_decision(
            action='page_oncall',
            params={'team': 'platform-team'},
            confidence=best['confidence'],
            reason=reason,
            ev=0.0,
            retrieval=retrieval_result,
            query_vec=query_vec,
            ev_table=ev_table,
        )

    # ── Auto-act ────────────────────────────────────────────────────────────
    chosen = best['action']
    params = _infer_params(chosen, query_vec, candidates)

    return _build_decision(
        action=chosen,
        params=params,
        confidence=best['confidence'],
        reason=(
            f"Top EV={best['ev']:.4f}: p_success={best['p_success']:.2f}, "
            f"blast={best['blast']}, similarity={max_sim:.3f}"
        ),
        ev=best['ev'],
        retrieval=retrieval_result,
        query_vec=query_vec,
        ev_table=ev_table,
    )


def _build_decision(
    action: str,
    params: dict,
    confidence: float,
    reason: str,
    ev: float,
    retrieval: dict,
    query_vec: dict,
    ev_table: list | None = None,
) -> dict:
    """Construct the full audit-ready decision dict."""
    return {
        'incident_id':     query_vec.get('incident_id', ''),
        'selected_action': action,
        'params':          params,
        'confidence':      round(confidence, 3),
        'consensus_score': retrieval.get('max_similarity', 0.0),
        'top_3_neighbors': retrieval.get('top_3_neighbors', []),
        'blast_radius_check': {
            'blast_gate':       BLAST_GATE,
            'confidence_gate':  CONFIDENCE_GATE,
            'min_confidence':   MIN_CONFIDENCE,
        },
        'evidence': {
            'reason':        reason,
            'ev_table':      ev_table or [],
            'is_ood':        retrieval.get('is_ood', False),
            'max_similarity': retrieval.get('max_similarity', 0.0),
            'affected_services': query_vec.get('affected_services', []),
            'top_error_edge':    query_vec.get('top_error_edge'),
            'candidates_raw':    retrieval.get('candidates', []),
        }
    }
