# graph_extractor — pipeline reference

Turns large document collections into a provenance-tracked node/edge graph. Two portable sources
run through one config-driven entry point; a third, full-scale source runs standalone.

Licensed under [Apache-2.0](LICENSE).

## Architecture

```
                     graph_extractor_universal.py
                        (edit one line: CONFIG = ...)
                                   │
                 ┌─────────────────┴─────────────────┐
                 ▼                                     ▼
   sources/wiki_html_sample.py             sources/europeana_edm.py
   data/wiki_sample/*.html                 data/europeana_sample/<id>/*.xml
   (43 real articles)      (one preset per dataset: 1433/215/1200/739)
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
geohistorical_extractor.py   (SOURCE_FORMAT = "zim" | "sample" — one config line picks the input)
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
| `sources/wiki_html_sample.py` | Reads `data/wiki_sample/`; reuses `classify_node`/`extract_coords`/`extract_founding_year` from `geohistorical_extractor.py`. Also builds real hyperlink edges among the sample's own nodes. |
| `sources/europeana_edm.py` | Reads one Europeana EDM dataset dir; extracts `artifact`/`place`/`person` nodes. Optional creator resolution via Europeana's Entity API (cached). |
| `sources/base.py` | Shared `ExtractedNode`/`ExtractedEdge`/`SourceAdapter` shapes. |
| `graph_export.py` | Writes the db to JSON and to a pyvis HTML visualization. |
| `geohistorical_extractor.py` | The real pipeline, `SOURCE_FORMAT`-selectable (`"zim"` needs the 124GB archive + libzim; `"sample"` needs neither). Self-contained (`utils/`, `names_search.py` both local). |
| `utils/extract_links.py` | Hyperlink extraction, shared by the wiki sample and the full pipeline. Strips navbox and bare flag-icon list noise (see Gotchas). |
| `names_search.py` | Fuzzy+embedding `NameMatcher`, used by `geohistorical_extractor.py`'s disambiguator-linking pass. |
| `data_decription.md` | Per-dataset breakdown: main nodes, subnodes, what's actually extractable. |
| `docs/EUROPEANA_ADAPTER_DESIGN.md` | Europeana field-reliability deep dive (why `dc:coverage` ≠ `dcterms:spatial`, etc.). |
| `gazetteer_match.py` + `sources/country_names.txt`/`sources/region_names.txt` | Simple word-matching alternative for a dataset with no other structure to key off — see below. |

## Gazetteer word-matching (`gazetteer_match.py`)

`sources/country_names.txt`/`sources/region_names.txt` are this project's own verified results — every `country`/
`region` the wiki sample actually classified (18 countries, 1 region), not a fabricated or
externally-sourced list. `find_countries(text)`/`find_regions(text)`/`find_all(text)` do plain
word-boundary matching against them — no per-format parsing, so it works on any free text
regardless of source. Genuinely useful for a dataset with no other structure to key off (unlike
wiki's infobox cascade or Europeana's RDF predicates). Demonstrated working on data neither list
was built from: run `python gazetteer_match.py` after generating `EUROPEANA_1200_CONFIG`'s output —
it finds "France" mentioned in 4 of that dataset's 126 nodes (via a creator field), a real match on
a dataset with zero wiki content in it.

## CONFIG presets (`graph_extractor_universal.py`)

| Preset | Source | Needs | Produces (verified) |
|---|---|---|---|
| `WIKI_SAMPLE_CONFIG` (default) | 43 real wiki articles | nothing external | 43 nodes, 104 edges |
| `EUROPEANA_1433_CONFIG` | prints/engravings, Italy | nothing external | 11 records → artifact/place nodes |
| `EUROPEANA_215_CONFIG` | text docs, Ireland | nothing external | 8 records |
| `EUROPEANA_1200_CONFIG` | digitized books, Cyprus | nothing external | 126 nodes, 78 edges (with cached creators) |
| `EUROPEANA_739_CONFIG` | fashion/jewelry collection | nothing external | 152 nodes, 92 edges |
| — run `geohistorical_extractor.py` directly, `SOURCE_FORMAT="zim"` | real Wikipedia | 124GB `.zim` + libzim | up to ~19M articles |
| — run `geohistorical_extractor.py` directly, `SOURCE_FORMAT="sample"` | same 43 wiki articles | nothing external | 43 nodes, 104 `link` + 253 `name_match` edges |

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

## Gotchas (real, found by running it)

- **Link extraction leaked navbox + flag-icon-list noise** — country articles "linked" to every
  other country in a shared G7/G20/EU-style navbox, or (Beijing) to ~175 countries via a bare
  "diplomatic relations" flag list. Fixed in `utils/extract_links.py`. Genuine mentions (e.g.
  France↔Italy sharing a real border) still come through.
- **A `PHASE1_ENTRY_LIMIT`-bounded classify pass never reaches "extract"/"link" on its own** —
  `run_classify_phase()` only advances past `phase="classify"` at the *true* end of the archive
  (correct for a resumable production run). A smoke test over a bounded window needs to
  force-advance `scan_progress` to `"extract"` manually right after the classify pass, rather
  than waiting for a literal full-archive scan, before calling `run_extract_phase`/`run_link_phase`.
- **`classify_node`'s keyword signal needs the real display title** ("New York City"), not an
  underscore filename — it substring-matches against prose, which uses spaces.
- **Europeana's `dcterms:spatial` ≠ `dc:coverage`** — the former is often the holding
  institution's location, not the record's subject. See `docs/EUROPEANA_ADAPTER_DESIGN.md`.
