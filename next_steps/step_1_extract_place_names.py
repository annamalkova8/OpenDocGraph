"""
Step 1 — extracts new place names from every graph node's own text (one LLM call per node, three
separate places/countries/regions lists) and merges them into graph.sqlite3, extending it beyond
what step_0's regex/infobox classifier found in the .zim directly.

Algorithm: dedupe extracted names across all source nodes, drop self-mentions and non-settlements,
optionally reclassify mis-bucketed country/region names, then match each remaining name against
the graph's own existing titles — an existing city gets a subnode edge, no match creates a new
city node (classification_source='llm_extracted'; a city-sourced mention gets no edge, only a
country/region/tribe/culture source does), and a match on an existing non-city node is dropped as
a mis-extraction. See READ_graph_bd.md for full rationale, edge cases, and known limitations.

Input:  common.DB_PATH (step_0's graph.sqlite3, every node with text; also written to)
        MAIN_DATASET_PATH (via step_0_geohistorical_extractor.build_name_matchers)
Output: common.DB_PATH (in place)            new nodes/edges this step finds
        OUTPUT_DIR/step_1_raw_by_node.json   per-source-node raw places/countries/regions
        OUTPUT_DIR/step_1_new_places.json    full report of what this step did
"""
import re
import sqlite3
import zlib

from common import (
    DB_PATH, OUTPUT_DIR, get_client, load_json, request_structured_json, run_pool, save_json,
    JSON_VALIDITY_RULE,
)

from FUND_restack.geohistorical_extractor import (
    NAME_CLASSIFIER_MIN_SCORE, STATE_NOISE_TERMS, _title_is_non_settlement, build_name_matchers,
    split_title_disambiguation,
)

RAW_OUTPUT_PATH = OUTPUT_DIR / "step_1_raw_by_node.json"
NEW_PLACES_REPORT_PATH = OUTPUT_DIR / "step_1_new_places.json"

SKIP_RECLASSIFY = True

MAX_WORKERS = 8
MAX_SOURCE_CHARS = 6000  # per-node text cap sent to the LLM, to avoid sending a full long article

PLACE_NAMES_SCHEMA = {
    "type": "object",
    "properties": {
        "places": {
            "type": "array",
            "items": {"type": "string"},
        },
        "countries": {
            "type": "array",
            "items": {"type": "string"},
        },
        "regions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["places", "countries", "regions"],
}

SYSTEM_PROMPT = (
    "You are a neutral, careful historical-text annotator. You extract only what "
    "the text actually names; you never invent places that are not mentioned."
)


def build_prompt(text):
    return (
        f"Take this text: {text}\n\n"
        "Extract three separate lists of names mentioned in the text:\n\n"
        "\"places\": every named settlement (city, town, village, or other inhabited place).\n"
        "\"countries\": every sovereign country, state, kingdom, or empire name (current or "
        "historical).\n"
        "\"regions\": every sub-national administrative subdivision (province, oblast, county, "
        "duchy, etc.) — not a full country and not a settlement.\n\n"
        "Rules:\n"
        "1. A name belongs to exactly one list — never put the same name in two lists (e.g. a "
        "country's name never also appears in \"places\", even if a city shares its name).\n"
        "2. Do not include people, tribes, rivers, buildings, organizations, events, dates, or "
        "adjectives in any list. This especially means: universities, colleges, schools, "
        "museums, libraries, markets, squares/plazas, monuments/statues/memorials, stadiums, "
        "airports, government bodies (ministries, parliaments, courts), companies, political "
        "parties, holidays, festivals, wars, battles, and treaties — even one named after, or "
        "located in, a settlement in the text. Only the settlement itself belongs in \"places\".\n"
        "3. Use the exact spelling/form the text uses for each name.\n"
        "4. List each name once per list, in the order it is first mentioned.\n"
        "5. If a list has nothing to include, return it as an empty list.\n"
        f"6. {JSON_VALIDITY_RULE}\n"
        "7. Return only the JSON object matching the schema, nothing else."
    )


def get_all_nodes_with_text(conn):
    """{path: {"title", "node_type", "text"}} for every node in the graph with its own article text."""
    rows = conn.execute(
        "SELECT path, title, node_type, text_compressed FROM nodes WHERE text_compressed IS NOT NULL"
    ).fetchall()
    return {
        path: {
            "title": title, "node_type": node_type,
            "text": zlib.decompress(blob).decode("utf-8")[:MAX_SOURCE_CHARS],
        }
        for path, title, node_type, blob in rows
    }


def process_node(path, node):
    client = get_client()
    text = node.get("text", "")
    if not text.strip():
        return path, {**node, "places": [], "countries": [], "regions": []}, None

    try:
        parsed = request_structured_json(
            client=client,
            schema=PLACE_NAMES_SCHEMA,
            schema_name="place_names",
            user_prompt=build_prompt(text),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=2000,
            broken_response_tag=f"step1.{path}",
        )
        updated = {
            **node,
            "places": parsed.get("places", []),
            "countries": parsed.get("countries", []),
            "regions": parsed.get("regions", []),
        }
        return path, updated, None
    except Exception as e:
        return path, {**node, "places": [], "countries": [], "regions": [], "error": str(e)}, e


def reclassify_node(node, country_matcher, region_matcher):
    """Moves a "place" that's actually a known country/region name into countries/regions."""
    countries = list(node.get("countries", []))
    regions = list(node.get("regions", []))
    kept_places = []

    for name in node.get("places", []):
        country_match = country_matcher.best_match(
            name, min_score=NAME_CLASSIFIER_MIN_SCORE, noise_terms=STATE_NOISE_TERMS
        )
        if country_match:
            canonical = country_match["name"]
            if canonical not in countries:
                countries.append(canonical)
            continue

        region_match = region_matcher.best_match(name, min_score=NAME_CLASSIFIER_MIN_SCORE)
        if region_match:
            canonical = region_match["name"]
            if canonical not in regions:
                regions.append(canonical)
            continue

        kept_places.append(name)

    return {**node, "places": kept_places, "countries": countries, "regions": regions}


def filter_non_settlements(node):
    """Drops any remaining "place" that looks like an institution/landmark/event, not a settlement."""
    kept_places = []
    excluded = list(node.get("excluded_non_settlements", []))

    for name in node.get("places", []):
        if _title_is_non_settlement(name):
            if name not in excluded:
                excluded.append(name)
        else:
            kept_places.append(name)

    return {**node, "places": kept_places, "excluded_non_settlements": excluded}


def normalize_name_key(name):
    return (name or "").strip().casefold()


_APOSTROPHE_STRIP_RE = re.compile("[’‘ʻʼʽ´`']")


def _strip_apostrophes(text):
    return _APOSTROPHE_STRIP_RE.sub("", text or "")


def filter_self_mentions(node):
    """Drops a node's own name (core name, apostrophe-insensitive) from its own extracted places — every article mentions itself."""
    title = node.get("title")
    self_core, _ = split_title_disambiguation(title) if title else (None, None)
    self_key = normalize_name_key(_strip_apostrophes(self_core))

    kept_places = []
    excluded_self = list(node.get("excluded_self_mentions", []))
    for name in node.get("places", []):
        core_name, _ = split_title_disambiguation(name)
        if self_key and normalize_name_key(_strip_apostrophes(core_name)) == self_key:
            if name not in excluded_self:
                excluded_self.append(name)
        else:
            kept_places.append(name)

    return {**node, "places": kept_places, "excluded_self_mentions": excluded_self}


def merge_and_link(results, conn):
    """Dedupes extracted place names, matches each against existing node titles, writes new nodes/edges, and returns the full per-place report."""
    mentions_by_norm = {}
    for source_path, node in results.items():
        source_title = node.get("title")
        source_node_type = node.get("node_type")
        for name in node.get("places", []):
            norm = normalize_name_key(name)
            if not norm:
                continue
            entry = mentions_by_norm.setdefault(norm, {"names": [], "mentions": []})
            if name not in entry["names"]:
                entry["names"].append(name)
            entry["mentions"].append({
                "source_path": source_path, "source_title": source_title,
                "source_node_type": source_node_type,
            })

    # title/core-name -> (path, node_type) index, to avoid creating a duplicate of an existing place
    exact_title_index = {}
    core_name_index = {}
    for path, title, node_type in conn.execute(
        "SELECT path, title, node_type FROM nodes WHERE title IS NOT NULL"
    ).fetchall():
        exact_title_index.setdefault(title, (path, node_type))
        core, _ = split_title_disambiguation(title)
        core_name_index.setdefault(core, []).append((path, node_type))

    def find_existing_match(name):
        match = exact_title_index.get(name)
        if match is None:
            candidates = core_name_index.get(name)
            if candidates and len(candidates) == 1:
                match = candidates[0]
        return match if match is not None else (None, None)

    report = {}
    new_node_rows = []
    new_edges = []

    for norm, entry in mentions_by_norm.items():
        canonical_name = entry["names"][0]
        existing_path, existing_type = find_existing_match(canonical_name)

        if existing_path is not None and existing_type != "city":
            report[f"skipped:{norm}"] = {
                "title": canonical_name,
                "status": "skipped_matches_noncity_node",
                "matched_path": existing_path,
                "matched_node_type": existing_type,
                "source_mentions": entry["mentions"],
            }
            continue

        if existing_path is not None:
            place_path, status_created = existing_path, False
        else:
            place_path, status_created = f"llm_extracted:{norm}", True

        edges_here = []
        for mention in entry["mentions"]:
            if mention["source_node_type"] == "city":
                continue  # city-sourced mention -> independent node, no edge
            target_path = mention["source_path"]
            if target_path == place_path:
                continue
            new_edges.append((place_path, target_path, "llm_extracted"))
            edges_here.append({"target_path": target_path, "target_title": mention["source_title"]})

        if status_created:
            new_node_rows.append((place_path, canonical_name, "city", "llm_extracted"))

        record = {
            "title": canonical_name,
            "status": "created" if status_created else ("existing_edge_added" if edges_here else "existing_no_change"),
            "source_mentions": entry["mentions"],
            "edges_added": edges_here,
        }
        if status_created:
            record["classification_source"] = "llm_extracted"
        report[place_path] = record

    if new_node_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO nodes (path, title, node_type, classification_source) "
            "VALUES (?, ?, ?, ?)",
            new_node_rows,
        )
    if new_edges:
        conn.executemany(
            "INSERT OR IGNORE INTO edges (source_path, target_path, edge_source) VALUES (?, ?, ?)",
            new_edges,
        )
    conn.commit()

    return report


def run_new_place_extraction(conn, use_cached_raw_only=False, skip_reclassify=SKIP_RECLASSIFY):
    """Runs extraction against an already-open connection and returns merge_and_link()'s full report; skip_reclassify skips the slow per-name country/region NameMatcher pass."""
    nodes = get_all_nodes_with_text(conn)
    print(f"Loaded {len(nodes)} graph nodes with text to scan for new place mentions")

    if use_cached_raw_only:
        # Skip the LLM and reuse whatever's already saved in RAW_OUTPUT_PATH (e.g. a prior partial run)
        results = load_json(RAW_OUTPUT_PATH, default={})
        print(f"Using {len(results)}/{len(nodes)} already-cached raw extraction(s) from "
              f"{RAW_OUTPUT_PATH} (use_cached_raw_only=True — not calling the LLM for the "
              f"remaining {len(nodes) - len(results)} node(s))")
    else:
        results = run_pool(
            items=nodes, worker=process_node, output_path=RAW_OUTPUT_PATH,
            max_workers=MAX_WORKERS, desc="step1-extract-new-places",
        )

    self_mentions = 0
    for path, node in results.items():
        before = len(node.get("places", []))
        results[path] = filter_self_mentions(node)
        self_mentions += before - len(results[path]["places"])
    print(f"Dropped {self_mentions} self-mentions (a node's own name found in its own text)")

    if skip_reclassify:
        print("Skipping country/region reclassify pass (skip_reclassify=True) — see this "
              "function's docstring for why that pass can be extremely slow")
    else:
        country_matcher, region_matcher = build_name_matchers()
        reclassified = 0
        for path, node in results.items():
            before = len(node.get("places", []))
            results[path] = reclassify_node(node, country_matcher, region_matcher)
            reclassified += before - len(results[path]["places"])
        country_matcher.save_cache()
        region_matcher.save_cache()
        print(f"Reclassified {reclassified} extracted \"places\" as known countries/regions "
              f"(min_score={NAME_CLASSIFIER_MIN_SCORE})")

    excluded = 0
    for path, node in results.items():
        before = len(node.get("places", []))
        results[path] = filter_non_settlements(node)
        excluded += before - len(results[path]["places"])
    print(f"Excluded {excluded} extracted \"places\" as non-settlements")

    report = merge_and_link(results, conn)
    save_json(report, NEW_PLACES_REPORT_PATH)

    created = sum(1 for r in report.values() if r["status"] == "created")
    existing_with_new_subnode = sum(1 for r in report.values() if r["status"] == "existing_edge_added")
    edges_added = sum(len(r.get("edges_added", [])) for r in report.values())
    skipped = sum(1 for r in report.values() if r["status"] == "skipped_matches_noncity_node")

    print("\n--- step 1 summary ---")
    print(f"  new city nodes added:                      {created}")
    print(f"  existing nodes that gained a subnode edge: {existing_with_new_subnode}")
    print(f"  total new edges added:                      {edges_added}")
    print(f"  extractions dropped (matched a non-city node): {skipped}")
    print(f"  -> {NEW_PLACES_REPORT_PATH}")

    return report


def open_db():
    """Returns a write connection to common.DB_PATH; raises if step 0 hasn't populated it yet."""
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"{DB_PATH} not found — run step_0_geohistorical_extractor.py (or multi_window_test.py) first."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def main():
    conn = open_db()
    run_new_place_extraction(conn)
    conn.close()


if __name__ == "__main__":
    main()
