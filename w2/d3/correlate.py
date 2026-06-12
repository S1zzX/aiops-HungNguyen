"""
correlate.py — Layer 1: Alert Correlation
Extracted từ w2/d1 notebook (assignment.ipynb).

Pipeline: session_groups → topology_group → emit clusters
"""
import json
import networkx as nx
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SEV_RANK = {'warn': 1, 'crit': 2}


# ---------------------------------------------------------------------------
# Graph builder — dùng đúng format services.json của bạn (có 'stores' + 'edges')
# ---------------------------------------------------------------------------

def build_graph(svc_data: dict) -> nx.DiGraph:
    """Build directed service graph từ services.json."""
    g = nx.DiGraph()
    for svc in svc_data['services']:
        g.add_node(svc['name'])
    for store in svc_data.get('stores', []):
        g.add_node(store['name'])
    for edge in svc_data['edges']:
        src = edge.get('from') or edge.get('src'); dst = edge.get('to') or edge.get('dst'); g.add_edge(src, dst, type=edge.get('type', 'call'))
    return g


def build_graph_from_file(path: str) -> tuple[nx.DiGraph, dict]:
    """Load services.json từ file, trả về (graph, raw_svc_data)."""
    svc_data = json.loads(Path(path).read_text(encoding='utf-8'))
    return build_graph(svc_data), svc_data


def build_graph_from_json(path: str) -> nx.DiGraph:
    """
    Generic graph loader — chấp nhận format đơn giản {services:[{id}], edges:[{src,dst}]}
    (dùng trong unit test) lẫn format services.json đầy đủ (services/stores/edges với from/to).
    Trả về chỉ graph (không kèm raw data) — dùng cho test_correlate.py.
    """
    data = json.loads(Path(path).read_text(encoding='utf-8'))

    g = nx.DiGraph()
    for svc in data.get('services', []):
        name = svc.get('name') or svc.get('id')
        g.add_node(name)
    for store in data.get('stores', []):
        g.add_node(store.get('name') or store.get('id'))
    for edge in data.get('edges', []):
        src = edge.get('from') or edge.get('src')
        dst = edge.get('to') or edge.get('dst')
        g.add_edge(src, dst, type=edge.get('type', 'call'))
    return g


# ---------------------------------------------------------------------------
# Core functions — copy nguyên xi từ notebook d1
# ---------------------------------------------------------------------------

def fingerprint(alert: dict) -> str:
    """Vân tay alert: service + metric + severity. Không include ts/value."""
    return f"{alert['service']}|{alert['metric']}|{alert['severity']}"


def session_groups(alerts: list[dict], gap_sec: int = 120) -> list[list[dict]]:
    """
    Session window: group alert liên tiếp nếu khoảng cách thời gian <= gap_sec.
    Chọn gap_sec=120 vì toàn bộ incident span chỉ ~6 phút, burst liên tục.
    """
    if not alerts:
        return []
    sorted_alerts = sorted(alerts, key=lambda a: a['ts'])
    groups = [[sorted_alerts[0]]]
    for alert in sorted_alerts[1:]:
        ts      = datetime.fromisoformat(alert['ts'].replace('Z', '+00:00'))
        last_ts = datetime.fromisoformat(groups[-1][-1]['ts'].replace('Z', '+00:00'))
        if (ts - last_ts).total_seconds() <= gap_sec:
            groups[-1].append(alert)
        else:
            groups.append([alert])
    return groups


def topology_group(alerts: list[dict], graph: nx.DiGraph, max_hop: int = 2) -> list[list[dict]]:
    """
    Union-Find grouping: 2 service cùng cluster nếu shortest_path <= max_hop.
    max_hop=2 giữ được cascade trực tiếp mà không kéo service không liên quan.
    """
    if not alerts:
        return []
    undirected = graph.to_undirected()
    by_service = defaultdict(list)
    for a in alerts:
        by_service[a['service']].append(a)
    services = list(by_service.keys())
    parent = {s: s for s in services}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for i, s1 in enumerate(services):
        for s2 in services[i+1:]:
            if s1 not in undirected or s2 not in undirected:
                continue
            try:
                dist = nx.shortest_path_length(undirected, s1, s2)
                if dist <= max_hop:
                    union(s1, s2)
            except nx.NetworkXNoPath:
                continue

    groups_dict = defaultdict(list)
    for s in services:
        groups_dict[find(s)].extend(by_service[s])
    return list(groups_dict.values())


def max_sev(group: list[dict]) -> str:
    return max(group, key=lambda a: SEV_RANK.get(a['severity'], 0))['severity']


def correlate(alerts: list[dict], graph: nx.DiGraph, gap_sec: int = 120, max_hop: int = 2) -> list[dict]:
    """Main pipeline: session_groups → topology_group → emit clusters."""
    sessions = session_groups(alerts, gap_sec=gap_sec)
    all_clusters = []
    for si, session in enumerate(sessions):
        topo_groups = topology_group(session, graph, max_hop=max_hop)
        for gi, group in enumerate(topo_groups):
            fps = sorted(set(fingerprint(a) for a in group))
            all_clusters.append({
                'cluster_id':   f'c-{si:03d}-{gi:03d}',
                'alert_count':  len(group),
                'services':     sorted(set(a['service'] for a in group)),
                'alert_ids':    sorted(a['id'] for a in group),
                'time_range':   [min(a['ts'] for a in group), max(a['ts'] for a in group)],
                'max_severity': max_sev(group),
                'fingerprints': fps,
                'alerts':       group,           # giữ lại để RCA dùng
                'severity_max': max_sev(group),  # alias cho RCA layer
                'first_ts':     min(a['ts'] for a in group),
                'last_ts':      max(a['ts'] for a in group),
            })
    return all_clusters
