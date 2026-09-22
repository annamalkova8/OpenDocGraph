"""JSON and pyvis-HTML writers for the pipeline's nodes/edges schema. See READ_graph_bd.md."""
import json
import sqlite3
from collections import Counter

NODE_TYPE_COLORS = {
    "city": "#4C78A8",
    "country": "#E45756",
    "region": "#F58518",
    "tribe": "#54A24B",
    "culture": "#B279A2",
    "artifact": "#72B7B2",
    "place": "#4C78A8",
    "person": "#E45756",
}
DEFAULT_NODE_COLOR = "#999999"
EDGE_COLORS = {
    "link": "#888888",
    "name_match": "#C9A227",
    "dc_coverage": "#72B7B2",
    "dc_creator": "#E45756",
}


def write_json(db_path, out_path):
    conn = sqlite3.connect(db_path)
    columns = [
        "path", "title", "node_type", "classification_source",
        "lat", "lon", "date_start", "date_end", "date_source", "extra_json",
    ]
    nodes = []
    for row in conn.execute(f"SELECT {', '.join(columns)} FROM nodes"):
        record = dict(zip(columns, row))
        record["extra"] = json.loads(record.pop("extra_json")) if record["extra_json"] else {}
        nodes.append(record)
    edges = [
        {"source": s, "target": t, "edge_source": e}
        for s, t, e in conn.execute("SELECT source_path, target_path, edge_source FROM edges")
    ]
    conn.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"nodes": nodes, "edges": edges}, f, ensure_ascii=False, indent=2)
    print(f"-> {out_path} ({len(nodes):,} nodes, {len(edges):,} edges)")


def write_visualization(db_path, out_path, max_nodes=1500, main_node_types=frozenset()):
    from pyvis.network import Network

    conn = sqlite3.connect(db_path)
    degree = Counter()
    edges = conn.execute("SELECT source_path, target_path, edge_source FROM edges").fetchall()
    for s, t, _ in edges:
        degree[s] += 1
        degree[t] += 1

    nodes = conn.execute(
        "SELECT path, title, node_type, classification_source, lat, lon, date_start, date_end "
        "FROM nodes"
    ).fetchall()
    conn.close()

    if len(nodes) > max_nodes:
        nodes.sort(key=lambda n: degree[n[0]], reverse=True)
        nodes = nodes[:max_nodes]
    kept_paths = {n[0] for n in nodes}
    edges = [(s, t, e) for s, t, e in edges if s in kept_paths and t in kept_paths]

    net = Network(height="900px", width="100%", directed=True, notebook=False, cdn_resources="in_line")
    net.barnes_hut(gravity=-3000, spring_length=120)

    for path, title, node_type, source, lat, lon, date_start, date_end in nodes:
        d = degree[path]
        tooltip_bits = [f"{title}", f"type: {node_type}", f"source: {source}"]
        if lat is not None:
            tooltip_bits.append(f"coords: {lat:.4f}, {lon:.4f}")
        if date_start is not None:
            date_bit = f"date: {date_start}" + (f"–{date_end}" if date_end != date_start else "")
            tooltip_bits.append(date_bit)
        tooltip_bits.append(f"edges: {d}")
        net.add_node(
            path,
            label=title or path,
            title="<br>".join(tooltip_bits),
            color=NODE_TYPE_COLORS.get(node_type, DEFAULT_NODE_COLOR),
            size=10 + min(d, 20) * 2,
            shape="diamond" if node_type in main_node_types else "dot",
        )

    for source, target, edge_source in edges:
        net.add_edge(
            source, target,
            color=EDGE_COLORS.get(edge_source, "#CCCCCC"),
            dashes=(edge_source == "name_match"),
            title=edge_source,
            arrows="to",
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    net.write_html(str(out_path), notebook=False)
    print(f"-> {out_path} ({len(nodes):,} nodes, {len(edges):,} edges shown, open in a browser)")
