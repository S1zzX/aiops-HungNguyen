"""
rca.py — Layer 2+3: Root Cause Analysis
Uses graph-based PageRank scoring + optional LLM enrichment to identify root cause.
"""
import hashlib
import json
import logging
import os
from cachetools import TTLCache

import networkx as nx

logger = logging.getLogger("aiops")

# ---------------------------------------------------------------------------
# LLM cache (TTL 1 hour — same prompt → same answer within an hour)
# ---------------------------------------------------------------------------
_llm_cache: TTLCache = TTLCache(maxsize=1000, ttl=3600)

# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------
USE_LLM = os.environ.get("AIOPS_USE_LLM", "true").lower() == "true"


# ---------------------------------------------------------------------------
# Graph-based RCA
# ---------------------------------------------------------------------------

def rca_graph(
    cluster: dict,
    G: nx.DiGraph,
) -> list[tuple[str, float]]:
    """
    Score services in the cluster by PageRank on the REVERSE subgraph
    of alerting services.

    Intuition: the root cause tends to be the node most things depend on
    (i.e. highest PageRank in the reverse dependency graph — many point to it).

    Returns: sorted list of (service, score) descending.
    """
    alerting_services = list(cluster["services_set"] if "services_set" in cluster
                             else set(cluster["services"]))

    # Build subgraph of alerting services (forward deps)
    subgraph_nodes = [s for s in alerting_services if s in G]
    if not subgraph_nodes:
        # Fallback: return first service with equal weight
        return [(s, 1.0 / len(alerting_services)) for s in alerting_services]

    sub = G.subgraph(subgraph_nodes)
    rev = sub.reverse(copy=True)

    if rev.number_of_nodes() == 0:
        return [(s, 1.0 / len(alerting_services)) for s in alerting_services]

    try:
        scores = nx.pagerank(rev, alpha=0.85, max_iter=100)
    except nx.PowerIterationFailedConvergence:
        scores = {n: 1.0 / rev.number_of_nodes() for n in rev.nodes()}

    # Normalize
    total = sum(scores.values()) or 1.0
    ranked = sorted(
        [(svc, score / total) for svc, score in scores.items()],
        key=lambda x: x[1],
        reverse=True,
    )
    return ranked


# ---------------------------------------------------------------------------
# Similar incident matching (simple keyword overlap)
# ---------------------------------------------------------------------------

def find_similar_incidents(
    cluster: dict,
    history: list[dict],
    top_k: int = 3,
) -> list[dict]:
    """
    Find historically similar incidents by matching affected services.
    Returns top_k incidents sorted by overlap ratio.
    """
    alerting = set(cluster["services_set"] if "services_set" in cluster
                   else cluster["services"])

    scored = []
    for inc in history:
        affected = set(inc.get("affected_services", []))
        if not affected:
            continue
        overlap = len(alerting & affected) / len(alerting | affected)
        if overlap > 0:
            scored.append((overlap, inc))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {"id": inc["id"], "similarity": round(score, 2), "summary": inc["summary"]}
        for score, inc in scored[:top_k]
    ]


# ---------------------------------------------------------------------------
# LLM enrichment
# ---------------------------------------------------------------------------

def _build_llm_prompt(cluster: dict, graph_candidates: list[tuple[str, float]], history: list[dict]) -> str:
    candidates_str = "\n".join(
        f"  - {svc}: confidence {score:.2f}" for svc, score in graph_candidates[:3]
    )
    similar = find_similar_incidents(cluster, history, top_k=2)
    similar_str = "\n".join(
        f"  - {s['id']}: {s['summary']}" for s in similar
    ) or "  None found"

    alerts_summary = ", ".join(
        f"{a['service']} ({a['metric']}={a['value']})" for a in cluster["alerts"][:5]
    )

    return f"""You are an SRE root cause analysis assistant.

INCIDENT CLUSTER:
- Alert count: {cluster['alert_count']}
- Affected services: {', '.join(cluster['services'] if 'services' in cluster else list(cluster['services_set']))}
- Alerts: {alerts_summary}
- Time range: {cluster['time_range'][0]} to {cluster['time_range'][1]}

GRAPH-BASED ROOT CAUSE CANDIDATES:
{candidates_str}

SIMILAR PAST INCIDENTS:
{similar_str}

Respond ONLY with a JSON object (no markdown, no explanation) in this exact format:
{{
  "root_cause": "<service-id>",
  "class": "<failure class, e.g. connection_pool_exhaustion>",
  "confidence": <0.0-1.0>,
  "reasoning": "<1-2 sentences explaining why>",
  "actions": ["<action 1>", "<action 2>", "<action 3>"],
  "similar_incidents": ["<incident-id>"]
}}"""


def call_llm_rca(prompt: str) -> dict:
    """Call Anthropic/OpenAI LLM. Cached by prompt hash."""
    cache_key = hashlib.sha256(prompt.encode()).hexdigest()

    if cache_key in _llm_cache:
        logger.info("LLM cache hit")
        return _llm_cache[cache_key]

    try:
        # Try OpenAI first
        from openai import OpenAI
        client = OpenAI(timeout=10.0, max_retries=1)
        response = client.chat.completions.create(
            model=os.environ.get("AIOPS_LLM_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=500,
        )
        raw = response.choices[0].message.content.strip()
        result = json.loads(raw)
        _llm_cache[cache_key] = result
        return result

    except Exception as e:
        logger.warning(f"LLM call failed: {e}")
        raise


# ---------------------------------------------------------------------------
# Graph-only fallback RCA (no LLM)
# ---------------------------------------------------------------------------

def _graph_only_rca(cluster: dict, G: nx.DiGraph, history: list[dict]) -> dict:
    candidates = rca_graph(cluster, G)
    top_svc, top_score = candidates[0] if candidates else ("unknown", 0.0)

    similar = find_similar_incidents(cluster, history)
    actions = []
    for inc in similar:
        inc_id = inc["id"]
        for h in history:
            if h["id"] == inc_id:
                actions = h.get("actions_taken", [])
                break
        if actions:
            break

    return {
        "root_cause": top_svc,
        "class": "unknown",
        "confidence": round(top_score, 2),
        "reasoning": f"Graph PageRank analysis identified {top_svc} as the most likely root cause "
                     f"(score={top_score:.2f}). LLM enrichment disabled.",
        "actions": actions or ["Investigate service logs", "Check recent deployments", "Review dependency graph"],
        "similar_incidents": [s["id"] for s in similar],
        "method": "graph-only",
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_rca(
    cluster: dict,
    all_alerts: list[dict],
    G: nx.DiGraph,
    history: list[dict],
) -> dict:
    """
    Full RCA: graph scoring → optional LLM enrichment.
    Falls back to graph-only if LLM is disabled or fails.
    """
    # Normalize cluster format (handle both services list and services_set)
    if "services_set" not in cluster:
        cluster = {**cluster, "services_set": set(cluster.get("services", []))}
    if "alerts" not in cluster:
        svcs = cluster["services_set"]
        cluster = {**cluster, "alerts": [a for a in all_alerts if a["service"] in svcs]}

    graph_candidates = rca_graph(cluster, G)
    top_svc, top_score = graph_candidates[0] if graph_candidates else ("unknown", 0.0)

    # Skip LLM if flag off or confidence already high
    if not USE_LLM:
        logger.info("LLM disabled via AIOPS_USE_LLM flag, using graph-only RCA")
        return _graph_only_rca(cluster, G, history)

    if top_score >= 0.9:
        logger.info(f"Graph confidence {top_score:.2f} >= 0.9, skipping LLM")
        return _graph_only_rca(cluster, G, history)

    # Try LLM enrichment
    try:
        prompt = _build_llm_prompt(cluster, graph_candidates, history)
        result = call_llm_rca(prompt)

        # Merge similar incidents from history search
        similar = find_similar_incidents(cluster, history)
        if not result.get("similar_incidents"):
            result["similar_incidents"] = [s["id"] for s in similar]

        result["method"] = "graph+llm"
        return result

    except Exception as e:
        logger.error(f"LLM enrichment failed, falling back to graph-only: {e}")
        return _graph_only_rca(cluster, G, history)
