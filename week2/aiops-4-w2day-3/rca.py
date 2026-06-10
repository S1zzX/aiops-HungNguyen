"""
rca.py — Layer 2+3: Root Cause Analysis
Extracted từ w2/d2 notebook (assignment_with_bonus.ipynb).

Graph scoring (rca_combined) + retrieval (retrieve_similar_incidents)
+ LLM enrichment via Groq API (llama-3.3-70b-versatile).
"""
import hashlib
import json
import logging
import os
import urllib.request
from datetime import datetime

import networkx as nx
from cachetools import TTLCache

logger = logging.getLogger('aiops')

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

USE_LLM    = os.environ.get('AIOPS_USE_LLM', 'true').lower() == 'true'
GROQ_KEY   = os.environ.get('GROQ_API_KEY', '')
GROQ_MODEL = os.environ.get('AIOPS_LLM_MODEL', 'llama-3.3-70b-versatile')

VALID_CLASSES = {
    'connection_pool_exhaustion', 'slow_query', 'memory_leak', 'rebalance_storm',
    'deadlock', 'network_partition', 'bad_deploy', 'config_push', 'tls_expiry',
    'ddos', 'thread_starvation', 'other',
}

# LLM response cache — same prompt → cached 1 giờ
_llm_cache: TTLCache = TTLCache(maxsize=1000, ttl=3600)


# ---------------------------------------------------------------------------
# Graph scoring — copy nguyên xi từ notebook d2
# ---------------------------------------------------------------------------

def rca_combined(cluster: dict, G: nx.DiGraph, w_graph: float = 0.6, w_time: float = 0.4) -> list[tuple[str, float]]:
    alerting_svcs = set(cluster['services'])
    sub = G.subgraph([n for n in G.nodes if n in alerting_svcs])

    # Terminal score
    terminal_scores = {}
    for svc in alerting_svcs:
        out_deg = sum(1 for _, v in sub.out_edges(svc) if v in alerting_svcs)
        terminal_scores[svc] = 1.0 / (1 + out_deg)

    # PageRank on reversed subgraph
    rev_sub = sub.reverse()
    if len(rev_sub) > 0:
        try:
            pr = nx.pagerank(rev_sub, alpha=0.85, max_iter=200)
        except nx.PowerIterationFailedConvergence:
            pr = {n: 1 / len(rev_sub) for n in rev_sub.nodes}
    else:
        pr = {svc: 1.0 for svc in alerting_svcs}
    pr_max  = max(pr.values()) if pr else 1.0
    pr_norm = {k: v / pr_max for k, v in pr.items()}

    graph_score = {
        svc: 0.5 * terminal_scores.get(svc, 0) + 0.5 * pr_norm.get(svc, 0)
        for svc in alerting_svcs
    }

    # Temporal score — earlier = higher
    svc_first_ts = {}
    for a in cluster['alerts']:
        svc = a['service']
        ts  = datetime.fromisoformat(a['ts'].replace('Z', '+00:00'))
        if svc not in svc_first_ts or ts < svc_first_ts[svc]:
            svc_first_ts[svc] = ts

    if svc_first_ts:
        t_min = min(svc_first_ts.values())
        t_max = max(svc_first_ts.values())
        span  = (t_max - t_min).total_seconds() or 1.0
        temporal_score = {
            svc: 1.0 - (ts - t_min).total_seconds() / span
            for svc, ts in svc_first_ts.items()
        }
    else:
        temporal_score = {svc: 0.5 for svc in alerting_svcs}

    combined = {}
    for svc in alerting_svcs:
        g = graph_score.get(svc, 0.0)
        t = temporal_score.get(svc, 0.5)
        combined[svc] = round(w_graph * g + w_time * t, 4)

    return sorted(combined.items(), key=lambda x: x[1], reverse=True)


# ---------------------------------------------------------------------------
# Retrieval — copy nguyên xi từ notebook d2
# ---------------------------------------------------------------------------

def retrieve_similar_incidents(cluster: dict, history: dict, top_k: int = 3) -> list[tuple[dict, float]]:
    cluster_services = set(cluster['services'])
    cluster_sev      = cluster.get('severity_max', cluster.get('max_severity', 'warn'))
    scored = []
    for inc in history['incidents']:
        score = 0.0
        if inc.get('root_cause_service', inc.get('root_cause', '')) in cluster_services:
            score += 0.4
        overlap = cluster_services & set(inc.get('services_involved', inc.get('affected_services', [])))
        score   += min(0.2 * len(overlap), 0.4)
        inc_sev  = 'crit' if inc.get('severity', 'medium') == 'critical' else 'warn'
        if inc_sev == cluster_sev:
            score += 0.2
        if score >= 0.2:
            scored.append((inc, round(score, 2)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


# ---------------------------------------------------------------------------
# LLM enrichment — Groq API (copy từ notebook d2, thêm cache + timeout)
# ---------------------------------------------------------------------------

def build_rca_prompt(cluster: dict, graph_top3: list, similar_incidents: list) -> str:
    top3_str = '\n'.join(
        f'  {rank+1}. {svc} (score={score:.4f})'
        for rank, (svc, score) in enumerate(graph_top3)
    )
    similar_str = ''
    for inc, score in similar_incidents:
        similar_str += (
            f'  - {inc["id"]} [{inc["severity"]}] {inc["root_cause_class"]} '
            f'on {inc.get('root_cause_service', inc.get('root_cause', ''))}\n'
            f'    Summary: {inc["summary"]}\n'
            f'    Remediation: {inc["remediation"]}\n'
        )
    fingerprints_str = '\n'.join(f'  - {fp}' for fp in cluster.get('fingerprints', [])[:10])

    return f"""You are an expert SRE performing automated root cause analysis.

## Cluster: {cluster['cluster_id']}
Time window: {cluster.get('first_ts', '')} to {cluster.get('last_ts', '')}
Max severity: {cluster.get('severity_max', cluster.get('max_severity', 'warn'))}
Services in cluster: {', '.join(cluster['services'])}

## Graph + Temporal Scoring (top candidates)
{top3_str}

## Alert Fingerprints (sample)
{fingerprints_str}

## Most Similar Historical Incidents
{similar_str}

## Task
Based on the graph scoring, alert fingerprints, and historical incidents above, produce a JSON object with exactly these fields:
{{
  "root_cause": "<service name - must be one of the services listed above>",
  "class": "<one of: connection_pool_exhaustion, slow_query, memory_leak, rebalance_storm, deadlock, network_partition, bad_deploy, config_push, tls_expiry, ddos, thread_starvation, other>",
  "confidence": <float 0.0-1.0>,
  "reasoning": "<2-3 sentences explaining WHY this is the root cause, referencing specific signals>",
  "actions": ["<action 1>", "<action 2>", "<action 3>"]
}}

Return ONLY the JSON object, no markdown fences, no explanation outside the JSON."""


def call_groq_rca(prompt: str) -> dict:
    """Call Groq API. Cached by prompt hash."""
    cache_key = hashlib.sha256(prompt.encode()).hexdigest()
    if cache_key in _llm_cache:
        logger.info('LLM cache hit')
        return _llm_cache[cache_key]

    if not GROQ_KEY:
        raise ValueError('GROQ_API_KEY not set. Set env var hoặc dùng AIOPS_USE_LLM=false để bypass.')

    payload = json.dumps({
        'model':           GROQ_MODEL,
        'messages':        [{'role': 'user', 'content': prompt}],
        'max_tokens':      1000,
        'temperature':     0,
        'response_format': {'type': 'json_object'},
    }).encode('utf-8')

    req = urllib.request.Request(
        'https://api.groq.com/openai/v1/chat/completions',
        data=payload,
        headers={
            'Content-Type':  'application/json',
            'Authorization': f'Bearer {GROQ_KEY}',
            'User-Agent':    'python-requests/2.31.0',
        },
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    raw  = data['choices'][0]['message']['content']
    clean = raw.strip().lstrip('```json').lstrip('```').rstrip('```').strip()
    result = json.loads(clean)
    _llm_cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# Classify + validate — copy từ notebook d2
# ---------------------------------------------------------------------------

def classify_from_history(similar_incidents: list) -> tuple[str, list[str]]:
    if not similar_incidents:
        return 'other', ['Investigate manually']
    best_inc, _ = similar_incidents[0]
    cls = best_inc.get('root_cause_class', best_inc.get('class', 'other')) if best_inc.get('root_cause_class', best_inc.get('class', 'other')) in VALID_CLASSES else 'other'
    return cls, [best_inc.get('remediation', best_inc.get('summary', 'Investigate manually'))]


def validate_output(result: dict, cluster_services: set) -> list[str]:
    issues = []
    if result['root_cause'] not in cluster_services:
        issues.append('root_cause not in cluster')
    if result['class'] not in VALID_CLASSES:
        issues.append('invalid class')
    if not (0.0 <= result['confidence'] <= 1.0):
        issues.append('confidence out of range')
    if not result['actions']:
        issues.append('empty actions')
    return issues


# ---------------------------------------------------------------------------
# Graph-only fallback
# ---------------------------------------------------------------------------

def _graph_only_result(cluster: dict, G: nx.DiGraph, history: dict) -> dict:
    ranked     = rca_combined(cluster, G)
    root_cause = ranked[0][0] if ranked else cluster['services'][0]
    confidence = ranked[0][1] if ranked else 0.3
    graph_top3 = ranked[:3]
    similar    = retrieve_similar_incidents(cluster, history, top_k=3)
    root_class, actions = classify_from_history(similar)
    top_inc    = similar[0][0] if similar else None

    reasoning = (
        f"Graph RCA ranked {root_cause} top (score {confidence:.2f}). "
        f"Terminal in alerting subgraph + earliest alert. "
    )
    if top_inc:
        reasoning += f"Closest match: {top_inc['id']} — '{top_inc['summary'][:80]}'. Class: {root_class}."

    result = {
        'cluster_id':        cluster['cluster_id'],
        'graph_top3':        [[svc, score] for svc, score in graph_top3],
        'root_cause':        root_cause,
        'class':             root_class,
        'confidence':        round(confidence, 4),
        'actions':           actions,
        'reasoning':         reasoning,
        'similar_incidents': [inc['id'] for inc, _ in similar],
        'method':            'graph+retrieval',
    }

    issues = validate_output(result, set(cluster['services']))
    if issues:
        logger.warning(f'Validation issues: {issues} — fallback')
        result.update({
            'root_cause': cluster['services'][0],
            'class':      'other',
            'actions':    ['Investigate manually'],
            'method':     'graph-only-fallback',
        })
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_rca(cluster: dict, G: nx.DiGraph, history: dict) -> dict:
    """
    Full RCA pipeline:
      1. Graph scoring (rca_combined)
      2. Retrieval (retrieve_similar_incidents)
      3. LLM enrichment via Groq (nếu USE_LLM=true và GROQ_API_KEY có)
      Fallback: graph+retrieval nếu LLM fail hoặc bị tắt.
    """
    ranked     = rca_combined(cluster, G)
    graph_top3 = ranked[:3]
    similar    = retrieve_similar_incidents(cluster, history, top_k=3)

    if not USE_LLM:
        logger.info('LLM disabled via AIOPS_USE_LLM=false, dùng graph+retrieval')
        return _graph_only_result(cluster, G, history)

    if not GROQ_KEY:
        logger.warning('GROQ_API_KEY không có, fallback graph+retrieval')
        return _graph_only_result(cluster, G, history)

    try:
        prompt  = build_rca_prompt(cluster, graph_top3, similar)
        llm_out = call_groq_rca(prompt)

        result = {
            'cluster_id':        cluster['cluster_id'],
            'graph_top3':        [[svc, score] for svc, score in graph_top3],
            'root_cause':        llm_out.get('root_cause', ranked[0][0] if ranked else 'unknown'),
            'class':             llm_out.get('class', 'other'),
            'confidence':        round(float(llm_out.get('confidence', ranked[0][1] if ranked else 0.3)), 4),
            'actions':           llm_out.get('actions', ['Investigate manually']),
            'reasoning':         llm_out.get('reasoning', ''),
            'similar_incidents': [inc['id'] for inc, _ in similar],
            'method':            'graph+llm',
        }

        issues = validate_output(result, set(cluster['services']))
        if issues:
            logger.warning(f'LLM output validation issues: {issues} — fallback')
            return _graph_only_result(cluster, G, history)

        return result

    except Exception as e:
        logger.error(f'Groq LLM failed: {e} — fallback to graph+retrieval')
        return _graph_only_result(cluster, G, history)
