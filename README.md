# OpenDocGraph

Turns large document collections into a node/edge graph. Each node records its source path and the
classification signal that produced it. Two portable sources run through one config-driven entry
point; a third, full-scale source runs standalone. The current version is a working prototype; see
[Status and roadmap](#status-and-roadmap).

Licensed under [Apache-2.0](LICENSE).

## Quick start

```bash
git clone https://github.com/annamalkova8/OpenDocGraph.git && cd OpenDocGraph
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python graph_extractor_universal.py
```

Runs on the bundled 43-article sample, no downloads or API keys needed. Expected output: `43 nodes, 104 edges`.
Results land in `output/`: `universal_graph_wiki_sample.json` (the graph) and
`universal_graph_wiki_sample_viz.html` (open in a browser). The sqlite db is written to `~/graph_extractor_data/`.

To run a Europeana sample instead, change the line `CONFIG = WIKI_SAMPLE_CONFIG` in
`graph_extractor_universal.py` to e.g. `EUROPEANA_1200_CONFIG` (see [CONFIG presets](#config-presets-graph_extractor_universalpy)).

## What is deterministic and what is optional

- **Classification cascade (deterministic, offline):** structural markup → lead-sentence keyword
  patterns → coordinate presence, in that priority order. Each node records the signal that produced
  it (`classification_source`). No model is involved. The `name_match` edges (Phase 3) also come from
  deterministic string matching (Aho-Corasick), not from a model.
- **Disambiguator linking pass (optional, produces `disambiguator_match` edges):** `utils/names_search.py` combines fuzzy matching with a
  small local embedding model (`all-minilm`, run offline through [Ollama](https://ollama.com); not a
  generative model). It is not needed for the quick start. If Ollama is unreachable it falls back to
  fuzzy matching only.
- **Europeana creator resolution (optional, network):** `resolve_creators` queries Europeana's Entity
  API and caches results. Off by default (`resolve_creators=False`), so the default run is fully offline.

## Status and roadmap

| | Today (this repository) | Planned |
|---|---|---|
| Sources | Wikipedia-style structured HTML, Europeana EDM/RDF | more Europeana and structured-HTML datasets; plain text beyond the current grant plan |
| Extraction | Three-signal classification cascade; hyperlink and name-mention edges | events and relations from running text (later step) |
| Schema | SQLite `nodes`/`edges`; source path and `classification_source` per record | adapter name/version per record; schema version; PROV-O alignment |
| Validation | Manual checks on the committed samples | JSON Schema and SHACL validation command; automated test suite |
| Resolution | — | OpenRefine Reconciliation API endpoint; OWL-Time temporal model |
| Output | JSON and HTML visualisation | CLI, programmatic API, web platform, graph-database export adapter |
| Evaluation | Sample counts only | open fixtures (Wikidata sample, Europeana records) and a measured benchmark |


```
                     graph_extractor_universal.py
                        (edit one line: CONFIG = ...)
                                   │
                 ┌─────────────────┴─────────────────┐
                 ▼                                     ▼
   sources/wiki_html_sample.py             sources/europeana_edm.py
   data/wiki_sample/*.html                 data/europeana_sample/<id>/*.xml
   (43 real articles)      (one preset per dataset: 1433/1200)
                 │                                     │
                 └─────────────────┬───────────────────┘
                                   ▼
                    shared nodes/edges sqlite schema
                                   │
                       ┌───────────┴───────────┐
                       ▼                       ▼
              graph_export.write_json   graph_export.write_visualization
```

```
utils/geohistorical_extractor.py   (SOURCE_FORMAT = "zim" | "sample" — one config line picks the input)
  classify (+coords/founding-year) → extract text+links → name-match → [optional] disambiguator-link
  → ~/graph_extractor_data/graph.sqlite3
```

`SOURCE_FORMAT="sample"` runs this file's *real* phase functions (not a reimplementation) against
`data/wiki_sample/` via `SampleArchive`, a minimal `libzim.Archive`-compatible shim — same
classify/extract/link/disambiguator code either way. Cross-checked against
`sources/wiki_html_sample.py`'s independent link extraction: both produce the same 104 `link`
edges on the 43-article sample; running the real pipeline additionally gets Phase 3's 253
`name_match` edges, which the simpler sample adapter doesn't compute.

## Files

| File | Role |
|---|---|
| `graph_extractor_universal.py` | Config-driven entry point. Pick a `CONFIG` preset, press Run. |
| `sources/wiki_html_sample.py` | Reads `data/wiki_sample/`; reuses `classify_node`/`extract_coords`/`extract_founding_year` from `utils/geohistorical_extractor.py`. Also builds real hyperlink edges among the sample's own nodes. |
| `sources/europeana_edm.py` | Reads one Europeana EDM dataset dir; extracts `artifact`/`place`/`person` nodes. Optional creator resolution via Europeana's Entity API (cached). |
| `sources/base.py` | Shared `ExtractedNode`/`ExtractedEdge`/`SourceAdapter` shapes. |
| `graph_export.py` | Writes the db to JSON and to a pyvis HTML visualization. |
| `utils/geohistorical_extractor.py` | The real pipeline, `SOURCE_FORMAT`-selectable (`"zim"` needs the 124GB archive + libzim; `"sample"` needs neither). Self-contained (everything it imports lives in `utils/`). |
| `utils/extract_links.py` | Hyperlink extraction, shared by the wiki sample and the full pipeline. Strips navbox and bare flag-icon list noise (see Gotchas). |
| `utils/names_search.py` | Fuzzy+embedding `NameMatcher`, used by `utils/geohistorical_extractor.py`'s disambiguator-linking pass. |
| `data_decription.md` | Per-dataset breakdown: main nodes, subnodes, what's actually extractable. |
| `docs/EUROPEANA_ADAPTER_DESIGN.md` | Europeana field-reliability deep dive (why `dc:coverage` ≠ `dcterms:spatial`, etc.). |

## `utils/` — what each file does

| File | What it does | Used by |
|---|---|---|
| `geohistorical_extractor.py` | The full rule-based pipeline. Phase 1 classifies each article with the three-signal cascade (`classify_node`) and pulls coordinates (`extract_coords`) and founding year (`extract_founding_year`); Phase 2 extracts text and links; Phase 3 matches names between nodes; an optional last pass links disambiguation pages. Writes a sqlite graph to `~/graph_extractor_data/graph.sqlite3`. Reads either a real `.zim` or the bundled sample (`SampleArchive`, a small `libzim.Archive`-compatible shim). Run directly: `python utils/geohistorical_extractor.py`. | `sources/wiki_html_sample.py` imports its classifiers; also runnable standalone |
| `extract_links.py` | `extract_links_proper(html)`: returns the internal article links from the main content only, skipping external links, navboxes and bare flag-icon lists. | `geohistorical_extractor.py`, `sources/wiki_html_sample.py` |
| `names_search.py` | `NameMatcher`: finds near-duplicate or similar names with fuzzy matching (rapidfuzz), optionally combined with local embeddings (Ollama, `all-minilm`); falls back to fuzzy-only if Ollama is unavailable. `STATE_NOISE_TERMS` lists words stripped before matching. `python utils/names_search.py` runs a small demo. | `geohistorical_extractor.py` (the optional disambiguator pass only) |
| `__init__.py` | Marks `utils` as a package. | — |

## CONFIG presets (`graph_extractor_universal.py`)

| Preset | Source | Needs | Produces (checked manually) |
|---|---|---|---|
| `WIKI_SAMPLE_CONFIG` (default) | 43 real wiki articles | nothing external | 43 nodes, 104 edges |
| `EUROPEANA_1433_CONFIG` | prints/engravings, Italy | nothing external | 12 nodes, 8 edges (11 records) |
| `EUROPEANA_1200_CONFIG` | digitized books, Cyprus | nothing external | 88 nodes, 0 edges by default; 126 nodes, 78 edges with `resolve_creators=True` (needs network) |
| — run `utils/geohistorical_extractor.py` directly, `SOURCE_FORMAT="zim"` | real Wikipedia | 124GB `.zim` + libzim | not yet run at full scale — only partial runs so far, no verified throughput/count numbers |
| — run `utils/geohistorical_extractor.py` directly, `SOURCE_FORMAT="sample"` | same 43 wiki articles | nothing external | 43 nodes, 104 `link` + 253 `name_match` edges |

`CONFIG.extraction.main_node_types`/`sub_node_types`: informational only (drives a summary print
+ diamond-vs-dot styling in the visualization) — extraction logic itself is inherently
source-specific and can't be made generic by a config value. `extract_coords`/`extract_dates`/
`extract_places`/`resolve_creators`: real per-adapter skip-the-work toggles.

## Schema

```sql
nodes(path PK, title, node_type, classification_source,
      lat, lon, date_start, date_end, date_source,   -- date_* = astronomical years, BCE negative
      extra_json)                                     -- source-specific fields

edges(source_path, target_path, edge_source, PK(source_path, target_path, edge_source))
```

`node_type` vocabulary differs by source on purpose: `city`/`country`/`region`/`tribe`/`culture`
for wiki, `artifact`/`place`/`person` for Europeana.

## Data and licences

Code: [Apache-2.0](LICENSE). The bundled sample data is third-party and keeps its own terms:

| Sample | Source | Rights (from the records' `edm:rights`) |
|---|---|---|
| `data/wiki_sample/` | 43 English Wikipedia articles | CC BY-SA 4.0 (Wikipedia) |
| `data/europeana_sample/1200` | Europeana, University of Cyprus / Oslo (GrECI) | CC BY 4.0 |
| `data/europeana_sample/1433` | Europeana, Tenimento di San Luigi | No Copyright – Other Known Legal Restrictions |

The records are unmodified metadata used as test fixtures; see each record's own rights statement
before reusing it outside this repository.

## Maintainer and contributing

Maintained by Anna Malkova ([@annamalkova8](https://github.com/annamalkova8)). Questions, bug reports
and new-adapter proposals: please open a [GitHub issue](https://github.com/annamalkova8/OpenDocGraph/issues).
To add a source, implement the `SourceAdapter` interface in `sources/base.py` (see
`sources/europeana_edm.py` for a worked example) and add a preset in `graph_extractor_universal.py`.
