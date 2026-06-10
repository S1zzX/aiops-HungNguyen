"""
correlate.py — Layer 1: Alert Correlation
Groups incoming alerts into clusters based on:
  - Time proximity (alerts within gap_sec of each other)
  - Service graph proximity (services within max_hop hops)
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import networkx as nx


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_graph_from_json(path: str) -> nx.DiGraph:
    """Load services.json and build a directed service dependency graph."""
    data = json.loads(Path(path).read_text())
    G = nx.DiGraph()

    for svc in data.get("services", []):
        G.add_node(svc["id"], name=svc.get("name", svc["id"]))

    for edge in data.get("edges", []):
        G.add_edge(edge["src"], edge["dst"], weight=edge.get("weight", 1.0))

    return G


# ---------------------------------------------------------------------------
# Alert fingerprinting
# ---------------------------------------------------------------------------

def fingerprint(alert: dict) -> str:
    """
    Stable identity of an alert — excludes timestamp and value so duplicate
    firing alerts (same service+metric+severity) hash to the same fingerprint.
    """
    key = f"{alert['service']}|{alert['metric']}|{alert['severity']}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Core correlation logic
# ---------------------------------------------------------------------------

def _parse_ts(ts_str: str) -> datetime:
    """Parse ISO-8601 string → aware datetime (UTC)."""
    ts_str = ts_str.rstrip("Z") + "+00:00"
    return datetime.fromisoformat(ts_str)


def _services_within_hops(G: nx.DiGraph, service: str, max_hop: int) -> set[str]:
    """
    Return all services reachable from `service` within max_hop hops
    in either direction (upstream + downstream).
    """
    reachable: set[str] = {service}

    if service not in G:
        return reachable

    # Forward (downstream)
    for node, dist in nx.single_source_shortest_path_length(G, service, cutoff=max_hop).items():
        reachable.add(node)

    # Backward (upstream — reverse graph)
    G_rev = G.reverse(copy=False)
    for node, dist in nx.single_source_shortest_path_length(G_rev, service, cutoff=max_hop).items():
        reachable.add(node)

    return reachable


def correlate(
    alerts: list[dict],
    G: nx.DiGraph,
    gap_sec: int = 120,
    max_hop: int = 2,
) -> list[dict[str, Any]]:
    """
    Group alerts into incident clusters.

    Algorithm:
      1. Sort alerts by timestamp.
      2. Use union-find style grouping: an alert joins an existing cluster if
         - Its timestamp is within gap_sec of the cluster's latest alert, AND
         - Its service is within max_hop hops of any service already in the cluster.
      3. Build cluster metadata.

    Returns list of cluster dicts.
    """
    if not alerts:
        return []

    # Sort by time
    sorted_alerts = sorted(alerts, key=lambda a: _parse_ts(a["ts"]))

    clusters: list[dict] = []  # list of {alerts, services_set, latest_ts}

    for alert in sorted_alerts:
        alert_ts = _parse_ts(alert["ts"])
        alert_svc = alert["service"]
        nearby_svcs = _services_within_hops(G, alert_svc, max_hop)

        placed = False
        for cluster in clusters:
            # Time proximity check
            time_diff = (alert_ts - cluster["latest_ts"]).total_seconds()
            if time_diff > gap_sec:
                continue

            # Service graph proximity check
            if cluster["services_set"] & nearby_svcs:
                cluster["alerts"].append(alert)
                cluster["services_set"].add(alert_svc)
                cluster["latest_ts"] = max(cluster["latest_ts"], alert_ts)
                placed = True
                break

        if not placed:
            clusters.append({
                "alerts": [alert],
                "services_set": {alert_svc},
                "latest_ts": alert_ts,
            })

    # Format output
    result = []
    for i, cluster in enumerate(clusters):
        svcs = list(cluster["services_set"])
        timestamps = sorted(_parse_ts(a["ts"]) for a in cluster["alerts"])
        fp_set = frozenset(fingerprint(a) for a in cluster["alerts"])
        cluster_id = "CLU-" + hashlib.sha256(str(sorted(fp_set)).encode()).hexdigest()[:8].upper()

        result.append({
            "cluster_id": cluster_id,
            "alert_count": len(cluster["alerts"]),
            "services": svcs,
            "time_range": [
                timestamps[0].isoformat(),
                timestamps[-1].isoformat(),
            ],
            "alerts": cluster["alerts"],
        })

    return result
