"""
evidence_graph.py — Generate causal-chain evidence graphs for all 5 incidents.

Usage:
    python evidence_graph.py

Output:
    evidence_graph.png  (5 subplots, one per incident)

Each directed edge is labelled with the propagation mechanism derived from
traces.json, alerts.json, and deploy_log.json.
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import networkx as nx

# ---------------------------------------------------------------------------
# Incident causal chains (derived from data-pack evidence)
# ---------------------------------------------------------------------------

INCIDENTS = [
    {
        "id": "I-1",
        "title": "I-1 · 2026-03-05 14:20–15:10\nfx-api 503 + no-jitter retry storm",
        "color": "#d62728",
        "edges": [
            ("fx-api", "payment-svc",
             "503 response\n(fx_503_after_3_retries)"),
            ("payment-svc", "payment-svc\n[conn-pool]",
             "3 retries × no jitter\n→ pool saturation"),
            ("payment-svc\n[conn-pool]", "checkout-svc",
             "conn_pool_acquire_timeout"),
            ("checkout-svc", "redis-cache",
             "cache miss storm\n(hit-rate drop)"),
            ("checkout-svc", "frontend",
             "p99 > 1500 ms\nerror rate > 5%"),
        ],
        "root": "fx-api",
    },
    {
        "id": "I-2",
        "title": "I-2 · 2026-03-11 02:45–04:30\nnumpy 2.0 memory leak → OOM",
        "color": "#ff7f0e",
        "edges": [
            ("inventory-svc\nv2.5.0 deploy\n(numpy 2.0)", "inventory-svc\n[heap]",
             "memory leak\n(RSS growing)"),
            ("homepage_promo\ncarousel flag", "inventory-svc\n[heap]",
             "traffic surge\n(02:15 flag enabled)"),
            ("inventory-svc\n[heap]", "inventory-svc\n[GC]",
             "GC pause\n(gc_pause spans)"),
            ("inventory-svc\n[GC]", "checkout-svc",
             "stock_lookup timeout\n→ error rate > 3%"),
            ("inventory-svc\n[heap]", "inventory-svc\n[OOM]",
             "RSS > 2000 MB\n→ OOM kill (04:18)"),
        ],
        "root": "inventory-svc\nv2.5.0 deploy\n(numpy 2.0)",
    },
    {
        "id": "I-3",
        "title": "I-3 · 2026-03-17 11:15–12:00\nloyalty flag → unindexed RDS query",
        "color": "#2ca02c",
        "edges": [
            ("enable_loyalty_\nrecommendations\nflag (100%)", "payment-svc\n[loyalty_client]",
             "feature activated\nat 11:15"),
            ("payment-svc\n[loyalty_client]", "rds-orders",
             "SELECT * no-index scan\n180k rows_examined"),
            ("rds-orders", "rds-orders\n[CPU]",
             "CPU > 65%\nquery p99 > 1000 ms"),
            ("rds-orders\n[CPU]", "payment-svc\n[conn-pool]",
             "slow queries hold\nDB connections"),
            ("payment-svc\n[conn-pool]", "checkout-svc",
             "downstream_timeout\n→ p99 > 1000 ms"),
        ],
        "root": "enable_loyalty_\nrecommendations\nflag (100%)",
    },
    {
        "id": "I-4",
        "title": "I-4 · 2026-03-22 09:00–09:40\npp-api IP rotation, stale DNS in AZ-c",
        "color": "#9467bd",
        "edges": [
            ("pp-api vendor\nIP block rotated\n203→198.51.100/24", "payment-svc\n[AZ-c]",
             "stale DNS cache\nresolved_ip=203.0.113.10"),
            ("payment-svc\n[AZ-c]", "checkout-svc\n[AZ-c]",
             "connection_refused\n→ regional error rate > 20%"),
            ("checkout-svc\n[AZ-c]", "frontend",
             "checkout errors\nAZ-c traffic only"),
            ("payment-svc\n[AZ-a/b]", "pp-api",
             "OK – resolved new IP\n198.51.100.20"),
        ],
        "root": "pp-api vendor\nIP block rotated\n203→198.51.100/24",
    },
    {
        "id": "I-5",
        "title": "I-5 · 2026-03-27 06:00–06:30\nmTLS cert not_before clock skew",
        "color": "#1f77b4",
        "edges": [
            ("service-mesh\ncert rotation\n(06:00:15Z)", "checkout-svc\n[mTLS validator]",
             "not_before=06:00:15Z\nvalidator clock=-27s"),
            ("checkout-svc\n[mTLS validator]", "checkout-svc\n→ payment-svc",
             "certificate_not_yet_valid\nhandshake failure"),
            ("checkout-svc\n→ payment-svc", "checkout-svc",
             "mtls_failure\nerror rate > 20%"),
            ("checkout-svc", "frontend",
             "SyntheticCheckout\nfailing"),
        ],
        "root": "service-mesh\ncert rotation\n(06:00:15Z)",
    },
]

# ---------------------------------------------------------------------------
# Drawing helper
# ---------------------------------------------------------------------------

def draw_incident(ax, incident):
    G = nx.DiGraph()
    edge_labels = {}
    for src, dst, label in incident["edges"]:
        G.add_edge(src, dst)
        edge_labels[(src, dst)] = label

    root = incident["root"]
    color = incident["color"]

    node_colors = []
    for node in G.nodes():
        if node == root:
            node_colors.append(color)
        elif "OOM" in node or "CPU" in node or "conn-pool" in node or "heap" in node or "GC" in node:
            node_colors.append("#ffbb78")
        elif node in ("frontend", "checkout-svc", "checkout-svc\n[AZ-c]"):
            node_colors.append("#aec7e8")
        else:
            node_colors.append("#c7e9c0")

    try:
        pos = nx.planar_layout(G)
    except Exception:
        pos = nx.spring_layout(G, seed=42, k=2.5)

    nx.draw_networkx_nodes(G, pos, ax=ax, node_color=node_colors,
                           node_size=2200, alpha=0.92)
    nx.draw_networkx_labels(G, pos, ax=ax, font_size=6.5, font_weight="bold")
    nx.draw_networkx_edges(G, pos, ax=ax, arrows=True,
                           arrowstyle="-|>", arrowsize=18,
                           edge_color="#555555", width=1.6,
                           connectionstyle="arc3,rad=0.08",
                           min_source_margin=18, min_target_margin=18)
    nx.draw_networkx_edge_labels(G, pos, edge_labels=edge_labels, ax=ax,
                                 font_size=5.5, label_pos=0.45,
                                 bbox=dict(boxstyle="round,pad=0.2",
                                           fc="white", ec="none", alpha=0.7))

    ax.set_title(incident["title"], fontsize=8, fontweight="bold", pad=10,
                 color=color)
    ax.axis("off")

    # Legend
    root_patch = mpatches.Patch(color=color, label="Root cause")
    inter_patch = mpatches.Patch(color="#ffbb78", label="Intermediate component")
    effect_patch = mpatches.Patch(color="#aec7e8", label="Affected service")
    dep_patch = mpatches.Patch(color="#c7e9c0", label="Dependency / external")
    ax.legend(handles=[root_patch, inter_patch, effect_patch, dep_patch],
              loc="lower left", fontsize=5, framealpha=0.8)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    fig, axes = plt.subplots(2, 3, figsize=(22, 14))
    axes = axes.flatten()

    for i, inc in enumerate(INCIDENTS):
        draw_incident(axes[i], inc)

    # Hide the 6th unused subplot
    axes[5].set_visible(False)

    fig.suptitle(
        "ronki-shop — Causal Evidence Graphs for 5 Incidents (March 2026)",
        fontsize=13, fontweight="bold", y=1.01
    )
    plt.tight_layout(pad=2.0)
    out = "evidence_graph.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
