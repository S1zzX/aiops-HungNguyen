"""
correlator.py — Alert chain grouper for ronki-shop incident data.

Usage:
    python correlator.py ../data-pack/

Grouping heuristic
------------------
Two alerts are placed in the same chain when at least ONE of the following
explicit signals connects them (temporal proximity alone is NOT sufficient):

1. Shared service / dependency edge in topology.json
   If alert A fires on service X and alert B fires on service Y, and X depends
   on Y (or Y depends on X) in the topology graph, they may share a causal
   chain.

2. Shared component or metric namespace
   Alerts whose 'metric' or 'name' labels reference the same logical subsystem
   (e.g. both reference payment-svc connection-pool metrics).

3. Temporal proximity + topology link
   Both signals are used together: alerts must fire within a configurable
   time window (default 90 minutes) AND have a topology relationship.

The algorithm:
  a) Build a directed dependency graph from topology.json.
  b) For each alert, compute the set of services reachable from (or reaching)
     it in the dependency graph — the "blast radius".
  c) Sort alerts by fired_at ascending.
  d) Use union-find to merge alerts whose blast radii overlap AND that fired
     within CHAIN_WINDOW_SECONDS of each other.
  e) For each resulting group, elect the earliest-fired alert as root.
  f) Identify the originating service as the deepest upstream node with a
     non-INFO alert.
  g) Print the CHAIN output.
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CHAIN_WINDOW_SECONDS = 90 * 60  # 90 minutes


# ---------------------------------------------------------------------------
# Union-Find
# ---------------------------------------------------------------------------

class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1


# ---------------------------------------------------------------------------
# Topology helpers
# ---------------------------------------------------------------------------

def build_graph(topology):
    """Return adjacency sets: upstream[svc] = services that svc depends on,
    downstream[svc] = services that depend on svc."""
    upstream = defaultdict(set)
    downstream = defaultdict(set)
    for svc in topology.get("services", []):
        name = svc["name"]
        for dep in svc.get("deps", []):
            upstream[name].add(dep)
            downstream[dep].add(name)
    for tp in topology.get("third_party", []):
        name = tp["name"]
        # third-party nodes have no deps themselves
        _ = upstream[name]
        _ = downstream[name]
    return upstream, downstream


def reachable(node, graph, visited=None):
    """BFS reachability from node following graph edges."""
    if visited is None:
        visited = set()
    queue = [node]
    while queue:
        cur = queue.pop()
        if cur in visited:
            continue
        visited.add(cur)
        for nxt in graph.get(cur, set()):
            if nxt not in visited:
                queue.append(nxt)
    return visited


def blast_radius(service, upstream, downstream):
    """Return the set of services involved in a fault at 'service':
    everything downstream (callers that would be affected) plus the service
    itself."""
    affected = reachable(service, downstream)
    affected.add(service)
    # Also include direct dependencies so we can match against dependency alerts
    affected |= upstream.get(service, set())
    return affected


# ---------------------------------------------------------------------------
# Chain reconstruction
# ---------------------------------------------------------------------------

def find_originator(chain_alerts, upstream):
    """
    Among all services in the chain, find the deepest upstream service
    (fewest callers) with a non-INFO severity alert.  Falls back to
    earliest-fired service.
    """
    non_info = [a for a in chain_alerts if a["severity"] != "info"]
    candidates = non_info if non_info else chain_alerts
    # Rank by number of upstream deps (more deps = deeper in the stack)
    def upstream_depth(a):
        return len(upstream.get(a["service"], set()))
    candidates_sorted = sorted(candidates, key=lambda a: (-upstream_depth(a), a["_ts"]))
    return candidates_sorted[0]


def format_chain(chain_idx, alerts, upstream):
    root = min(alerts, key=lambda a: a["_ts"])
    originator = find_originator(alerts, upstream)
    children = [a for a in alerts if a["id"] != root["id"]]
    children.sort(key=lambda a: a["_ts"])

    root_ts = root["fired_at"]
    orig_ts = originator["fired_at"]

    lines = []
    lines.append(
        f"CHAIN {chain_idx}: {root['name']} ({root['service']}) @ {root_ts}"
    )

    # Originator block
    comp = originator.get("labels", {})
    metric = comp.get("metric", originator.get("name", "?"))
    upstream_ep = originator.get("labels", {}).get("check", metric)
    lines.append(f"  originator: {originator['service']}.{metric} @ {orig_ts}")
    lines.append(
        f"    first trouble: {originator['severity'].upper()} "
        f"{originator['summary']} (upstream: {upstream_ep})"
    )

    # Child alerts
    if children:
        lines.append("  child alerts:")
        root_dt = root["_ts"]
        for i, child in enumerate(children):
            delta = int((child["_ts"] - root_dt).total_seconds())
            connector = "+--" if i < len(children) - 1 else "\\--"
            lines.append(
                f"    {connector} {child['name']} ({child['service']}) (+{delta}s)"
            )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(data_dir: Path):
    # Load data
    with open(data_dir / "alerts.json") as f:
        alerts = json.load(f)
    with open(data_dir / "topology.json") as f:
        topology = json.load(f)

    upstream, downstream = build_graph(topology)

    # Parse timestamps
    for a in alerts:
        a["_ts"] = datetime.fromisoformat(a["fired_at"].replace("Z", "+00:00"))

    # Sort by time
    alerts.sort(key=lambda a: a["_ts"])

    n = len(alerts)
    uf = UnionFind(n)

    # Merge alerts that share a topology link and are within the time window
    for i in range(n):
        br_i = blast_radius(alerts[i]["service"], upstream, downstream)
        for j in range(i + 1, n):
            delta = (alerts[j]["_ts"] - alerts[i]["_ts"]).total_seconds()
            if delta > CHAIN_WINDOW_SECONDS:
                break  # sorted, so no later alert can be within the window
            br_j = blast_radius(alerts[j]["service"], upstream, downstream)
            # Explicit signal: overlapping blast radii mean a topology link exists
            if br_i & br_j:
                uf.union(i, j)

    # Group by root
    groups = defaultdict(list)
    for i, alert in enumerate(alerts):
        groups[uf.find(i)].append(alert)

    # Sort groups by earliest alert time
    sorted_groups = sorted(groups.values(), key=lambda g: min(a["_ts"] for a in g))

    for idx, group in enumerate(sorted_groups, start=1):
        print(format_chain(idx, group, upstream))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python correlator.py <data-pack-dir>")
        sys.exit(1)
    main(Path(sys.argv[1]))
