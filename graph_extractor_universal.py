import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import graph_export

# =============================================================================
# CONFIG
# =============================================================================


@dataclass
class WikiSampleConfig:
    sample_dir: str = ""
    extract_coords: bool = True
    extract_dates: bool = True


@dataclass
class EuropeanaConfig:
    dataset_dirs: list = field(default_factory=list)
    extract_dates: bool = True
    extract_places: bool = True
    resolve_creators: bool = False


@dataclass
class ExtractionConfig:
    """Informational only — see README.md. Doesn't filter or change extraction."""
    main_node_types: frozenset = frozenset()
    sub_node_types: frozenset = frozenset()


@dataclass
class PipelineConfig:
    source_type: str  # "wiki_html_sample" | "europeana_edm"
    wiki_sample: WikiSampleConfig = field(default_factory=WikiSampleConfig)
    europeana: EuropeanaConfig = field(default_factory=EuropeanaConfig)
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)
    db_path: Path = Path.home() / "graph_extractor_data" / "universal_graph.sqlite3"
    output_formats: frozenset = frozenset({"sqlite"})  # {"sqlite", "json"}
    json_output_path: Path = Path(__file__).resolve().parent / "output" / "universal_graph.json"
    visualize: bool = False
    visualization_output_path: Path = (
        Path(__file__).resolve().parent / "output" / "universal_graph_visualization.html"
    )


_OUTPUT_DIR = Path(__file__).resolve().parent / "output"
_EUROPEANA_SAMPLE_DIR = Path(__file__).resolve().parent / "data" / "europeana_sample"
_WIKI_SAMPLE_DIR = Path(__file__).resolve().parent / "data" / "wiki_sample"

WIKI_SAMPLE_CONFIG = PipelineConfig(
    source_type="wiki_html_sample",
    wiki_sample=WikiSampleConfig(sample_dir=str(_WIKI_SAMPLE_DIR)),
    extraction=ExtractionConfig(
        main_node_types=frozenset({"city"}),
        sub_node_types=frozenset({"country", "region", "tribe", "culture"}),
    ),
    db_path=Path.home() / "graph_extractor_data" / "universal_graph_wiki_sample.sqlite3",
    output_formats=frozenset({"sqlite", "json"}),
    json_output_path=_OUTPUT_DIR / "universal_graph_wiki_sample.json",
    visualize=True,
    visualization_output_path=_OUTPUT_DIR / "universal_graph_wiki_sample_viz.html",
)


def _europeana_dataset_config(dataset_id):
    return PipelineConfig(
        source_type="europeana_edm",
        europeana=EuropeanaConfig(dataset_dirs=[str(_EUROPEANA_SAMPLE_DIR / dataset_id)]),
        extraction=ExtractionConfig(
            main_node_types=frozenset({"artifact"}),
            sub_node_types=frozenset({"place", "person"}),
        ),
        db_path=Path.home() / "graph_extractor_data" / f"universal_graph_europeana_{dataset_id}.sqlite3",
        output_formats=frozenset({"sqlite", "json"}),
        json_output_path=_OUTPUT_DIR / f"universal_graph_europeana_{dataset_id}.json",
        visualize=True,
        visualization_output_path=_OUTPUT_DIR / f"universal_graph_europeana_{dataset_id}_viz.html",
    )


EUROPEANA_1433_CONFIG = _europeana_dataset_config("1433")  # prints & engravings, Italy
EUROPEANA_1200_CONFIG = _europeana_dataset_config("1200")  # digitized early-modern books, Cyprus

CONFIG = WIKI_SAMPLE_CONFIG

# =============================================================================


def build_source(config):
    if config.source_type == "wiki_html_sample":
        from sources.wiki_html_sample import WikiHtmlSampleSource
        if not config.wiki_sample.sample_dir:
            raise ValueError("CONFIG.wiki_sample.sample_dir is empty — nothing to read")
        return WikiHtmlSampleSource(
            sample_dir=config.wiki_sample.sample_dir,
            extract_coords=config.wiki_sample.extract_coords,
            extract_dates=config.wiki_sample.extract_dates,
        )
    if config.source_type == "europeana_edm":
        from sources.europeana_edm import EuropeanaEdmSource
        if not config.europeana.dataset_dirs:
            raise ValueError("CONFIG.europeana.dataset_dirs is empty — nothing to read")
        return EuropeanaEdmSource(
            dataset_dirs=config.europeana.dataset_dirs,
            resolve_creators=config.europeana.resolve_creators,
            extract_dates=config.europeana.extract_dates,
            extract_places=config.europeana.extract_places,
        )
    raise ValueError(f"build_source() doesn't handle source_type: {config.source_type!r}")


def open_db(db_path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nodes (
            path TEXT PRIMARY KEY,
            title TEXT,
            node_type TEXT,
            classification_source TEXT,
            lat REAL,
            lon REAL,
            date_start INTEGER,
            date_end INTEGER,
            date_source TEXT,
            extra_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);

        CREATE TABLE IF NOT EXISTS edges (
            source_path TEXT,
            target_path TEXT,
            edge_source TEXT,
            PRIMARY KEY (source_path, target_path, edge_source)
        );
        """
    )
    conn.commit()
    return conn


def run(config):
    source = build_source(config)
    conn = open_db(config.db_path)

    node_count = 0
    type_counts = Counter()
    batch = []
    for node in source.iter_nodes():
        batch.append((
            node.path, node.title, node.node_type, node.classification_source,
            node.lat, node.lon, node.date_start, node.date_end, node.date_source,
            json.dumps(node.extra, ensure_ascii=False) if node.extra else None,
        ))
        type_counts[node.node_type] += 1
        node_count += 1
        if len(batch) >= 500:
            conn.executemany(
                "INSERT OR REPLACE INTO nodes (path, title, node_type, classification_source, "
                "lat, lon, date_start, date_end, date_source, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                batch,
            )
            conn.commit()
            batch = []
            if node_count % 5000 == 0:
                print(f"[{config.source_type}] {node_count:,} nodes written...")
    if batch:
        conn.executemany(
            "INSERT OR REPLACE INTO nodes (path, title, node_type, classification_source, "
            "lat, lon, date_start, date_end, date_source, extra_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            batch,
        )
        conn.commit()
    print(f"[{config.source_type}] {node_count:,} nodes total")

    known_paths = {row[0] for row in conn.execute("SELECT path FROM nodes")}
    edge_rows = [
        (e.source_path, e.target_path, e.edge_source) for e in source.iter_edges(known_paths)
    ]
    if edge_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO edges (source_path, target_path, edge_source) VALUES (?, ?, ?)",
            edge_rows,
        )
        conn.commit()
    print(f"[{config.source_type}] {len(edge_rows):,} edges total")

    conn.close()
    _print_main_sub_summary(type_counts, config.extraction)


def _print_main_sub_summary(type_counts, extraction):
    if not extraction.main_node_types and not extraction.sub_node_types:
        return
    main_count = sum(n for t, n in type_counts.items() if t in extraction.main_node_types)
    sub_count = sum(n for t, n in type_counts.items() if t in extraction.sub_node_types)
    unclassified = {
        t: n for t, n in type_counts.items()
        if t not in extraction.main_node_types and t not in extraction.sub_node_types
    }
    print(f"[main/sub] main ({sorted(extraction.main_node_types)}): {main_count:,} | "
          f"sub ({sorted(extraction.sub_node_types)}): {sub_count:,}")
    if unclassified:
        print(f"[main/sub] node_type(s) not declared as main or sub in CONFIG.extraction: "
              f"{dict(unclassified)}")


def main():
    run(CONFIG)

    if "json" in CONFIG.output_formats:
        graph_export.write_json(CONFIG.db_path, CONFIG.json_output_path)

    if CONFIG.visualize:
        graph_export.write_visualization(
            CONFIG.db_path, CONFIG.visualization_output_path,
            main_node_types=CONFIG.extraction.main_node_types,
        )


if __name__ == "__main__":
    main()
