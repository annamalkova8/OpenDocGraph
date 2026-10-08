# Universality: adding Europeana as a second source

Written against `utils/geohistorical_extractor.py` (see `README.md` for the pipeline this feeds).
Research + design only — no changes to the production script yet.

> **Note on bundled data:** this document records research on four Europeana datasets (1433, 215, 1200,
> 739). Only `1433` and `1200` ship in `data/europeana_sample/`; `215` and `739` contain CC BY-NC-ND
> material and were removed from the repository. Their findings are kept below as research notes.

## 1. How to load Europeana data

Europeana exposes the same underlying data (EDM — Europeana Data Model, RDF-based) through four
access paths, with a real tradeoff between them:

| Method | What you get | Cost to use | Fit here |
|---|---|---|---|
| **Anonymous FTP dataset dump** | One `.zip` of EDM RDF/XML per dataset ID, `ftp://download.europeana.eu/dataset/XML/<id>.zip` (+ `.md5sum`) | Free, no key, no protocol — plain `curl`/`wget` | **Validated below.** Simplest path to a real, small, redistributable fixture for tests — pick a dataset by record count from the daily `status_*.csv` index |
| **Search + Record REST API** | JSON, one query/record at a time, cursor-paginated | Free API key (self-serve at pro.europeana.eu); a public demo key `api2demo` also works for exploration (used below), rate-limited | Best for a *filtered* slice (by `edm:type`, provider, or `dcterms:spatial`) rather than a whole dataset — and for **finding which dataset IDs** to pull via FTP, since the FTP listing itself carries no type/subject metadata |
| **OAI-PMH harvester** | EDM RDF/XML, `europeana-oai-pmh-import-*.json`, 1000 records/file | Free, but a *full* harvest is 250+ GB and takes over a week; can be scoped to one dataset/provider/date range | Right tool once the adapter is proven and we want scale comparable to the Wikipedia benchmark |
| **SPARQL endpoint** | RDF, arbitrary graph queries against the whole dataset | Free, no download at all | Good for exploring relationships (e.g. "which records point at this place") during entity-resolution work, not for bulk ingestion |

### Validated: FTP dump, real sample

Downloaded and checksum-verified `dataset 1433` (11 records, ~52 KB zipped) into
`data/europeana_sample/` — chosen by scanning `status_2026_01_19.csv` (the FTP server's own daily
dataset-status index, `DatasetId,FileStatus,TotalRecords,FailedRecords`) for the smallest
`REHARVESTED` (active) dataset. Parsing it directly confirms the mapping proposed in §3:

- Each record's `dc:coverage` (inside `ore:Proxy`) points at one or more `edm:Place` URIs.
- Each `edm:Place` carries `wgs84_pos:lat`/`long`, a `skos:prefLabel` (often multilingual), and a
  `dcterms:isPartOf` link to its parent place — a real, ready-made hierarchy. For dataset 1433's
  image records this resolved to `Turin → Centro-Crocetta (neighborhood)` and, separately,
  `Torino → Italy`, both with coordinates, no guessing involved.
- Real edge case found immediately: the dataset's 3 video records carry **no** `dc:coverage` at
  all — a chunk of Europeana content has no resolvable place and needs a different attachment path
  (e.g. provider/organization metadata) than the "attach to a place" mapping below.

For Milestone 1's "second corpus" test, this FTP path is the pragmatic default: pick a handful of
small-to-medium datasets across different providers/countries via the status CSV, no API key
request or rate-limit budgeting needed to get started. Reserve the Search API for later, filtered
sampling, and OAI-PMH/full harvest for an eventual at-scale run once the adapter is proven.

**Caveat found while doing this**: the published `<id>.zip.md5sum` for datasets 215 and 1200
(below) didn't match the freshly-downloaded zip, even though `unzip -t` reports the zip itself as
internally valid and a second, independent download was byte-identical to the first. Read as a
stale checksum file on Europeana's end (dumps get reharvested; the top-level md5sum apparently
doesn't always get regenerated in lockstep), not a corrupted transfer — but worth a note for anyone
scripting this: verify with `unzip -t` / re-download-and-diff, don't trust the `.md5sum` blindly.

### Validated: TEXT-type datasets, and why `edm:type=TEXT` doesn't mean "here is the text"

Used the Search API with the public demo key to find real `TYPE:TEXT` datasets, since the FTP
listing alone carries no content-type info:

```
GET https://api.europeana.eu/record/v2/search.json?wskey=api2demo&query=*&qf=TYPE:TEXT&qf=LANGUAGE:en&rows=50&profile=minimal
```

— then took each result's `id` field (`/1200/...`) apart to get its dataset number, cross-checked
record counts against a HEAD request on the FTP zip, and pulled the two smallest:
**dataset 215** (Royal Irish Academy / Digital Repository of Ireland, 8 records, ~28 KB) and
**dataset 1200** (University of Cyprus, 88 records of digitized early-modern Greek/Latin printed
books, ~680 KB). Only dataset 1200 is kept in `data/europeana_sample/` (215 was removed for licence reasons).

Two things this surfaced that materially change §2/§3 below:

1. **`edm:Place` in a TEXT record is often the holding institution's location, not the document's
   subject.** In dataset 1200, every record's `edm:Place` resolves to `Bayerische Staatsbibliothek →
   München` — i.e. *where the physical book is archived today* (Munich), regardless of what the
   17th-century Greek grammar text is actually about or where it was printed. Dataset 1433's
   `edm:Place` (§ above) was genuinely about the artwork's subject/creation. There's no field-level
   way to tell these apart across providers — `edm:Place` is used inconsistently for provenance vs.
   subject, and a "second corpus" test has to account for that rather than assume one behavior.
   Dataset 215 shows the opposite failure mode: only 2 of 8 records have any `edm:Place` at all;
   the rest carry `dcterms:spatial` as **free text** ("Ireland", "European Union") or a Library of
   Congress authority URI with no coordinates — closer to Wikipedia's keyword signal (needs
   resolving against a gazetteer) than to a ready-made hierarchy.
2. **The EDM record is metadata *about* the object, not the object's text.** `edm:type=TEXT` marks
   the kind of thing being described; the actual document body (when digitized) lives behind a
   separate `edm:WebResource` flagged `rdf:type=edm:FullTextResource` — usually a PDF link (10 of
   dataset 215's 8 records carry one; none of dataset 1200's do). Getting real body text for
   classification/entity-extraction means a second fetch (and, for a PDF with no OCR layer, actual
   OCR) per record — there's no "lead paragraph" sitting in the metadata the way there is in a
   Wikipedia HTML page.

Net effect: for TEXT-type Europeana content, place-classification confidence and full-text
availability both vary by *provider*, not just by record — the adapter's `NodeClassifier` for
Europeana can't be a single uniform rule; it needs to report its own confidence/provenance per
record (which the `classification_source` column already supports) and treat "no usable place" and
"no fetchable text" as normal, common outcomes rather than edge cases.

## 2. Why this isn't a drop-in "same signals, different loader" swap

The three-signal cascade in `classify_node()` (infobox class → lead-sentence keyword → geo-coords)
exists because Wikipedia gives us *unstructured HTML prose* and the job is to infer structure from
it. Europeana's EDM records are the opposite: already-typed, already-structured metadata
(`edm:type` ∈ {TEXT, IMAGE, SOUND, VIDEO, 3D}, `dc:type`, `dc:subject`, `dcterms:spatial`,
`edm:place` with its own lat/long + preferred label, `dc:coverage`). There's no lead sentence to
pattern-match — the classification problem the cascade solves doesn't exist for this source.

The bigger structural difference: this pipeline is **city-centered** — cities are the nodes,
everything else (`country`/`region`/`tribe`/`culture`) attaches to a city by link or name mention.
A Europeana record is a heritage *object* (a painting, a book, a newspaper page), not a place. Most
records aren't candidate nodes at all in this schema — they're candidate **edges**: a record whose
`edm:place`/`dcterms:spatial` resolves to a place already in the graph becomes a
`edge_source='europeana_provenance'` edge from that place to a new `artifact` node (or, if we don't
want artifact nodes yet, straight into that place's provenance/text record). Only when a record's
`edm:place` names a place *not yet in the graph* does it produce a new node — and even then, the
new node's `node_type`/coordinates come directly from `edm:place`'s own structured fields, not from
re-running keyword/coords guesses against prose.

So Europeana exercises a different part of the same design than the ZIM source does: it's a real
stress test for the **adapter interface, entity resolution and provenance model** (the actual
Milestone 1/2 asks), not for the classification cascade itself. Worth stating that plainly in the
grant text rather than implying it's the same kind of document being run through the same
classifier — a reviewer who checks would notice the mismatch.

## 3. Proposed adapter interface

```python
class SourceRecord(NamedTuple):
    id: str                 # ZIM path, or Europeana record ID (e.g. "/2020601/...")
    title: str
    payload: Any            # raw HTML string (ZIM) or parsed EDM dict (Europeana)
    source_format: str      # "wiki_html" | "edm_json"

class SourceAdapter(Protocol):
    def iter_records(self) -> Iterator[SourceRecord]: ...
    def total_count(self) -> int | None: ...          # None if unknown up front (e.g. API cursor)

class NodeClassifier(Protocol):
    def classify(self, record: SourceRecord) -> tuple[str | None, str | None]:
        """Returns (node_type, classification_source) or (None, None)."""
```

- `ZimWikiSource` + `WikiInfoboxClassifier` — thin wrappers around the existing
  `libzim.Archive` iteration and `classify_node()`; behavior unchanged.
- `EuropeanaSource` — iterates the Search API cursor (or OAI-PMH dump, later), yields one
  `SourceRecord` per EDM record with `source_format="edm_json"`.
- `EuropeanaPlaceClassifier` — reads `edm:Place`/`dcterms:spatial` directly; no regex cascade, but
  not a single uniform rule either (see the TEXT-dataset findings above — `edm:Place` can mean
  "subject place" or "current holding institution" depending on provider, and it's sometimes
  absent). Where a record's place already matches an existing graph node (by name/coords), it
  emits an edge, not a node — this classifier's contract is slightly wider than `NodeClassifier`
  above since it can also report "attach to existing node X" rather than only "new node of type
  Y". Its `classification_source` should distinguish `edm_place_coords` (structured, high
  confidence) from `dcterms_spatial_text` (free text, needs gazetteer resolution, lower
  confidence) — the ZIM side's three-tier confidence tagging generalizes here directly, just with
  different tiers.

`run_classify_phase()` would take a `SourceAdapter` + `NodeClassifier` pair instead of hardcoding
`libzim.Archive` and `classify_node()`; everything downstream (DBWriter, schema, Phase 2/3) is
already source-agnostic since it only deals with `nodes`/`edges` rows.

## 4. Suggested next step

Prototype `EuropeanaSource` standalone (a script that pulls ~500–1000 records via the Search API
and prints the proposed node/edge mapping) before wiring it into `utils/geohistorical_extractor.py` — lets
us validate the field mapping against real records without touching the production path. Ask before
starting that prototype, since it needs a Europeana API key to be requested first.

*(§4 is now superseded by the adapter actually being built — see §5. Left as-is for history.)*

## 5. Museum objects: dataset 739, creator entity resolution, and what's genuinely not there

Looked specifically for a dataset rich enough to build a real object→creator→place graph from
(the ask: "connect objects with halls, museums, cities, authors, years"). Findings, in order of
how they change the picture:

**Found**: dataset 739, a cross-institution fashion/jewelry/costume collection (Rijksmuseum is the
largest single contributor, alongside a few Austrian/Dutch museums) — 3,766 records full size, trimmed to 151 for testing (every ~25th kept for spread across sub-collections); not bundled in
this repository (licence). Picked via the Search API's `DATA_PROVIDER` facet restricted to
`contentTier:4`/`metadataTier:A` (Europeana's own quality tiers), which surfaced it as a genuinely
well-curated, high-volume, image-heavy dataset rather than a random guess.

**Author/year is real and resolvable, via a mechanism not used before this**: `dc:creator` on
these records is sometimes a URI (`http://data.europeana.eu/agent/<id>`), and Europeana's
**Entity API** (`api.europeana.eu/entity/agent/base/<id>.json`) resolves it to actual structured
biography — `dateOfBirth`/`dateOfDeath` (same astronomical-year, BCE-negative convention already
used for wiki founding years), profession, and dozens of cross-references (Wikidata/VIAF/GND/LOC).
Verified against real data, not the spec: resolving agent 166818 from dataset 1200 correctly
returns Herodotus, born −483, died −424; across datasets 1200+739, 39 distinct creators resolved
correctly, including Homer (−900/−800), Plato (−428/−348), Martin Luther (1483/1546) — all
accurate. **Where the resolvable URI actually lives was a real gotcha**: it's on the
**europeana_proxy** (Europeana's own normalized aggregation), not the provider_proxy where the
readable creator *name* sits — the provider_proxy's own creator URI (when present) is typically a
VIAF/GND link, not a Europeana agent URI, so it isn't resolvable through this API at all. Missing
this split first produced zero resolutions during testing even though the code "looked" correct.

**Object creation dates are often ranges, not points**: `dcterms:created` on this dataset commonly
reads `"c.1750 - c.1800"`. The existing single-year extraction (built for wiki founding years and
Europeana's other date fields) would have silently collapsed that to `1800` alone via its
take-the-last-number heuristic — added a dedicated range parser (handles the "c." prefix on either
side and BC/BCE) that runs before the single-year fallback.

**What's genuinely not there, checked rather than assumed**: **hall/room/gallery-level location**
(`edm:currentLocation`) doesn't appear in any of the datasets checked (Rijksmuseum, Albertina, this
fashion collection). That granularity lives in museums' own internal collection-management
systems, not in what gets published to Europeana as EDM. So "connect objects to the *halls* they're
in" isn't achievable from this data source — "connect objects to the *museum* and its city/country"
is (via `edm:dataProvider`/`edm:provider` + `edm:country`), and that's the honest ceiling here.

**Resulting graph shape**, implemented in `sources/europeana_edm.py`:
```
artifact --[dc_creator]--> person (title, birth/death year via Entity API, profession)
artifact --[dc_coverage]--> place (when a coordinate-bearing subject place exists)
artifact.date_start/date_end   <- dcterms:created (point or range), or dc:date/edm:year/dcterms:issued
artifact.extra.data_provider/provider  <- the actual museum + the aggregator that fed Europeana
```
Creator resolution is opt-in (`EuropeanaConfig.resolve_creators`, off by default) since it's the
one place this adapter touches the network — every resolution is cached to
`output/europeana_entity_cache.json` keyed by creator URI, so a later run with the flag left off
still gets every previously-resolved person for free, and only a genuinely new creator URI would
need it turned on again.
