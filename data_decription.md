# Data description

One entry per source dataset this pipeline can read. "Main nodes" / "subnodes" follows the same
sense used throughout the pipeline: a main node is the primary entity the graph organizes around;
a subnode attaches to a main node by an edge rather than standing on its own. Everything here is
extracted deterministically (regex / XML-field reads) — no LLM, and network use is called out
explicitly where it applies (only Europeana creator resolution).

Shared node/edge shape across every dataset below (see `graph_extractor_universal.py`):
`path, title, node_type, classification_source, lat, lon, date_start, date_end, date_source,
extra_json`. `date_start`/`date_end` use astronomical year numbering (BCE negative, e.g. `-483` =
483 BC).

---

## Wikipedia `.zim` (`wikipedia_en_all_maxi_2026-02.zim`)

**Short description**: full English Wikipedia dump in ZIM archive format — 27.2M entries, ~19M
real articles, original HTML markup (infoboxes, coordinate microformats, lead prose) intact. Read
directly by `utils/geohistorical_extractor.py` (standalone; needs the real 124GB archive + libzim, so
it isn't wired into `graph_extractor_universal.py` — see the wiki sample entry below for the
portable equivalent).

**Main nodes**: `city` — settlement-type articles (towns, cities, villages, hamlets...). This
pipeline is deliberately city-centered.

**Subnodes**: `country`, `region`, `tribe`, `culture` — attach to a city by hyperlink
(`edge_source='link'`) or name mention (`edge_source='name_match'`), never to each other directly
(city-to-city links are excluded as near-always editorial/etymological, not real relationships).

**What can be extracted, for both nodes and subnodes**:
- `node_type` + `classification_source` — via a 3-signal cascade, strongest first: infobox class
  (`ib-settlement`, `ib-country`, ...), lead-sentence keyword ("X is a city/kingdom/..."), or bare
  geo-coordinate presence (weakest, gated against institution/landmark titles).
- `lat`/`lon` — from Wikipedia's Coord template (`href="geo:lat,lon"`); present on almost every
  classified node (231/232 in the tested sample).
- `date_start`/`date_end` — best-effort founding year from an infobox row
  (Founded/Established/Settled/Incorporated/Formation, excluding "...by" rows), including BCE
  dates. Sparse: most articles don't carry a clean row for this (8/232 in the tested sample) — a
  `NULL` date here is the expected outcome, not a failure.
- Full article text and every outbound hyperlink (Phase 2), city-to-city links dropped.
- Name-cross-reference edges between city text and non-city titles, and vice versa (Phase 3).
- Optional pass (off by default, needs a local Ollama daemon): disambiguator-linking, matching a
  city's ", Region" suffix to an in-graph region node.

---

## Wiki sample (`data/wiki_sample/`) — 43 real articles, no `.zim` needed

**Short description**: the full `.zim` is 124GB and not something most people running this repo
(a grant reviewer included) will have. This is 43 real article HTML pages extracted from it once
and committed to the repo, read by `sources/wiki_html_sample.py` through the exact same
`classify_node`/`extract_coords`/`extract_founding_year` functions the full-archive pipeline
uses — a working, checkable instance of the wiki path with zero external data dependency. Picked
for feature coverage, not randomly: the original 10 curated (Bologna, New York City, Detroit,
Rome, Istanbul, France, Italy, Yorkshire, Clovis culture, Inca Empire) plus 5 natural "coords"-tier
examples, then expanded with 13 more major world cities (Berlin, Madrid, London, Tokyo, Beijing,
Cairo, Athens, Vienna, Moscow, Sydney, Toronto, Mexico City, Buenos Aires) each paired with its own
country, plus Scotland and Wales. No `tribe` example was found among ~20 candidate searches across
both extraction passes — not fabricated just to complete the set. See
`data/wiki_sample/manifest.json` for the precomputed expected result per article.

**Main nodes**: same as the full `.zim` — `city`.

**Subnodes**: `country`, `region`, `culture`.

**What can be extracted**: identical to the full `.zim` above (same functions, real data) —
`node_type`/`classification_source` across all 3 signal tiers, `lat`/`lon` on most nodes, and real
founding years including two BCE ones (Rome `-753`, Beijing `-1045`) and a genuine Neolithic site
(`'En Esur`, `-5000`). Also produces real hyperlink edges among the sample's own nodes — 104 edges
across 43 nodes, a real directed graph, not isolated nodes.

**Real bug found and fixed via this sample** (see `README.md`'s Gotchas — this affects the
full production pipeline too, not just the sample): `utils/extract_links.py`'s link extraction
never stripped navbox elements the way text extraction already did, so country articles linked to
every other country listed in a shared membership navbox (G7/G20/EU/"sovereign states of Europe")
regardless of whether their prose actually mentioned each other — plus a second, narrower pattern
(bare flag-icon + country-name list entries, e.g. a capital city's "diplomatic relations" list)
with the same effect. Both are now stripped before link extraction; the genuine relationships that
remain (e.g. France↔Italy sharing a real border, mentioned in actual prose) are still there.

---

## Europeana dataset `1433` — Tenimento San Luigi / CulturaItalia (Italy)

**Short description**: small Italian cultural association's collection of prints and engravings,
via the CulturaItalia aggregator. 11 records (8 images, 3 videos).

**Main nodes**: `artifact` — one per EDM record (a print, an engraving, a video).

**Subnodes**: `place` — only when `dc:coverage` links to a coordinate-bearing `edm:Place`; this is
the one dataset where that signal was clean end to end (Turin → Italy, both with real coordinates
and a `dcterms:isPartOf` hierarchy).

**What can be extracted**:
- `artifact`: title, creator (free text), `edm:type` (IMAGE/VIDEO), rights license URI,
  data provider + provider, creation date (`dc:date` pointing at a Wikidata year-concept, e.g.
  "1816" — resolved via an inline `skos:Concept`, not literal text).
- `place`: label, lat/lon, parent place via `isPartOf` — connected to its artifact by a
  `dc_coverage` edge.
- Real gap found: the 3 video records have no `dc:coverage` at all — no place, no date. Some
  fraction of any Europeana dataset will simply have nothing to attach.

---

## Europeana dataset `1200` — University of Cyprus (digitized early-modern books)

**Short description**: 88 digitized early-modern Greek/Latin printed books (Herodotus, Homer,
Plato, Aristotle, Erasmus editions, etc.), `TEXT` type.

**Main nodes**: `artifact` — one per digitized book/page record.

**Subnodes**:
- `place` — present via `edm:Place`, but usually means the *holding library's city* (e.g. Munich,
  via the Bavarian State Library), not the book's subject — distinguished by role
  (`"custody"` vs `"spatial"`) using a `dcterms:provenance` text match, not promoted to an edge.
- `person` — the real payoff of this dataset: `dc:creator` links to a resolvable
  `data.europeana.eu/agent/<id>` URI on the *europeana_proxy*, resolved via Europeana's **Entity
  API** into real biography. Confirmed correct against real historical figures: Herodotus
  (−483/−424), Homer (−900/−800), Plato (−428/−348), Martin Luther (1483/1546).

**What can be extracted**:
- `artifact`: title(s), rights, provider ("University of Cyprus"), creation date (`dc:date` via a
  Wikidata year-concept, same mechanism as dataset 1433).
- `person` (needs `resolve_creators=True`, one cached network call per new creator): name,
  `date_start`/`date_end` = birth/death year, profession/occupation concept URIs. Connected to its
  artifact by a `dc_creator` edge.
- `place`: label + coordinates when present, tagged `custody` (not asserted as a real subject
  relationship).

---

## Datasets examined but not bundled

Two further Europeana datasets (`215`, Documents on Irish Foreign Policy; `739`, a fashion/jewelry
collection) were examined during adapter design (see `docs/EUROPEANA_ADAPTER_DESIGN.md`) but are not
included in this repository: their records are, in whole or part, CC BY-NC-ND 4.0, which does not fit
an Apache-2.0 repository meant for free reuse. The adapter handles their fields in code; re-download
them from Europeana if you want to run them.

---

## Cross-dataset notes

- **`node_type` vocabulary differs by source on purpose**: `city`/`country`/`region`/`tribe`/
  `culture` for wiki vs. `artifact`/`place`/`person` for Europeana — forcing one shared taxonomy
  across such different data (place-classification prose vs. object metadata) would misrepresent
  what's actually being classified. See `docs/EUROPEANA_ADAPTER_DESIGN.md` §2 for the full
  reasoning.
- **Confidence tiers are the right lens for comparing datasets**, not raw field presence: a
  `dc:coverage`-derived place (1433) is trustworthy; a `dcterms:spatial`-derived one (1200) is
  not, even though both datasets technically have "a place field."
- **No dataset here gives hall/room-level museum location.** If that granularity is a hard
  requirement, it will need a different source (a museum's own API/open dataset, not Europeana's
  aggregated EDM).
