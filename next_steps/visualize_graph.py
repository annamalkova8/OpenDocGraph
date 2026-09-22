"""
Render a graph.sqlite3 as an interactive pyvis/vis.js network diagram: a structural view (no
coordinates), node color = node_type, edge style = edge_source (solid = link, dashed =
name_match), node size = degree. Meant for test-sized DBs — MAX_NODES caps what's drawn, keeping
the highest-degree nodes.
"""
import sqlite3
from collections import Counter
from pathlib import Path

from pyvis.network import Network

# =============================================================================
# CONFIG
# =============================================================================

# Defaults to multi_window_test.py's test DB; point at graph_extractor.DB_PATH for a real run.
DB_PATH = Path.home() / "graph_extractor_data" / "test_graph.sqlite3"

OUTPUT_HTML_PATH = Path(__file__).resolve().parent / "output" / "graph_visualization.html"

# Safety cap — physics layout gets unusable well before 1M nodes; highest-degree nodes kept.
MAX_NODES = 1500

# Include zero-edge non-city (orphan) nodes? Toggle off for a cleaner diagram of just connected structure.
INCLUDE_ORPHAN_NODES = True

NODE_TYPE_COLORS = {
    "city": "#4C78A8",     # blue — the main/organizing nodes
    "country": "#E45756",  # red
    "region": "#F58518",   # orange
    "tribe": "#54A24B",    # green
    "culture": "#B279A2",  # purple
}
DEFAULT_NODE_COLOR = "#999999"

EDGE_COLORS = {
    "link": "#888888",        # literal hyperlink (Phase 2) — solid, higher confidence
    "name_match": "#C9A227",  # name cross-reference (Phase 3) — dashed, lower confidence
}

# =============================================================================


def load_graph(db_path, max_nodes):
    conn = sqlite3.connect(db_path)

    degree = Counter()
    for source, target in conn.execute("SELECT source_path, target_path FROM edges"):
        degree[source] += 1
        degree[target] += 1

    all_nodes = conn.execute(
        "SELECT path, title, node_type, classification_source FROM nodes"
    ).fetchall()

    if not INCLUDE_ORPHAN_NODES:
        all_nodes = [n for n in all_nodes if degree[n[0]] > 0]

    if len(all_nodes) > max_nodes:
        print(f"{len(all_nodes):,} nodes exceeds MAX_NODES={max_nodes:,} — keeping the "
              f"{max_nodes:,} highest-degree nodes (most structurally interesting)")
        all_nodes.sort(key=lambda n: degree[n[0]], reverse=True)
        all_nodes = all_nodes[:max_nodes]

    kept_paths = {n[0] for n in all_nodes}
    edges = [
        (source, target, edge_source)
        for source, target, edge_source in conn.execute(
            "SELECT source_path, target_path, edge_source FROM edges"
        )
        if source in kept_paths and target in kept_paths
    ]

    conn.close()
    return all_nodes, edges, degree


def build_network(nodes, edges, degree):
    net = Network(height="900px", width="100%", directed=True, notebook=False, cdn_resources="in_line")
    net.barnes_hut(gravity=-3000, spring_length=120)

    for path, title, node_type, classification_source in nodes:
        d = degree[path]
        net.add_node(
            path,
            label=title or path,
            title=f"{title}<br>type: {node_type}<br>source: {classification_source}<br>edges: {d}",
            color=NODE_TYPE_COLORS.get(node_type, DEFAULT_NODE_COLOR),
            size=10 + min(d, 20) * 2,  # capped so one hub node doesn't dwarf everything else
        )

    for source, target, edge_source in edges:
        net.add_edge(
            source,
            target,
            color=EDGE_COLORS.get(edge_source, "#CCCCCC"),
            dashes=(edge_source == "name_match"),
            title=edge_source,
            arrows="to",
        )

    return net


def print_legend(nodes, edges):
    type_counts = Counter(n[2] for n in nodes)
    edge_counts = Counter(e[2] for e in edges)
    print(f"Rendering {len(nodes):,} nodes, {len(edges):,} edges")
    print("  node_type:  " + ", ".join(f"{t}={c:,}" for t, c in type_counts.most_common()))
    print("  edge_source:" + ", ".join(f"{s}={c:,}" for s, c in edge_counts.most_common()))
    print("  colors: " + ", ".join(f"{t}={NODE_TYPE_COLORS.get(t, DEFAULT_NODE_COLOR)}" for t in type_counts))
    print("  edges: solid line = link (hyperlink), dashed line = name_match (cross-reference)")


def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"{DB_PATH} doesn't exist — run multi_window_test.py or "
                                 f"step_1_graph_extractor.py first, or point DB_PATH at an existing one.")

    nodes, edges, degree = load_graph(DB_PATH, MAX_NODES)
    if not nodes:
        print(f"No nodes found in {DB_PATH} — nothing to visualize.")
        return

    print_legend(nodes, edges)
    net = build_network(nodes, edges, degree)

    OUTPUT_HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    net.write_html(str(OUTPUT_HTML_PATH), notebook=False)
    print(f"-> {OUTPUT_HTML_PATH}  (open in a browser)")


if __name__ == "__main__":
    main()
