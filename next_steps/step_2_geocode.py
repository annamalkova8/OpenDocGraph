"""
Step 2 — geocode each city node from the graph: article-grounded first, LLM-knowledge fallback
second, kept as separate coordinate fields. No dedup pass needed since Phase 1 already skips
.zim redirects, so each city node is already a unique place.

Input:  DB_PATH graph.sqlite3 city nodes with text
Output: step_2_geocoded_raw.json (per node), step_2_geocoded.json (re-keyed "{name}_{lat}_{lon}"),
        step_2_skipped_no_coords.json
"""
from common import (
    OUTPUT_DIR, get_client, request_structured_json, run_pool, resolve_dual,
    get_city_nodes, get_source_text_for_city, save_json,
)

RAW_OUTPUT_PATH = OUTPUT_DIR / "step_2_geocoded_raw.json"
OUTPUT_PATH = OUTPUT_DIR / "step_2_geocoded.json"
SKIPPED_OUTPUT_PATH = OUTPUT_DIR / "step_2_skipped_no_coords.json"

MAX_WORKERS = 8
MAX_SOURCE_CHARS = 3000  # coordinates are almost always near the top of the article

GEO_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "lat": {"type": "number"},
        "lon": {"type": "number"},
    },
    "required": ["found", "lat", "lon"],
}

ARTICLE_SYSTEM_PROMPT = (
    "You are a precise, source-grounded geography assistant. You only report coordinates that "
    "the given text actually states or clearly implies; you never invent a location from outside "
    "knowledge."
)

KNOWLEDGE_SYSTEM_PROMPT = (
    "You are a precise geography assistant. Answer with your best estimate of the real-world "
    "location from your own knowledge; set found=false only if you have no reasonable idea."
)


def build_article_prompt(title, source_text):
    return (
        f'Here is text about the settlement "{title}" — its own article, plus any related '
        f"country/region/tribe/culture articles the graph connected it to:\n\n{source_text}\n\n"
        "Based ONLY on this text, what are its geographic coordinates? Look for an explicit "
        "coordinate pair (e.g. in an infobox, often written like \"56.95°N 24.1°E\"), or a clear "
        "textual description of its location (e.g. \"on the coast of X\", \"20km north of Y\") that "
        "lets you derive one.\n"
        "Set \"found\" to true only if the text supports a location this way; set it to false and "
        "leave lat/lon as 0 if the text gives no locating information at all — do NOT fall back on "
        "what you already know about a place with this name."
    )


def build_knowledge_prompt(title):
    return (
        f'The source text gave no coordinates for the settlement "{title}". Provide your '
        "best-estimate geographic coordinates for it from your own knowledge. If it is a "
        "historical or former settlement, use its approximate historical/archaeological location. "
        "Set \"found\" to false only if you have no reasonable idea where this place is or could "
        "be (e.g. the name is too generic or ambiguous to locate)."
    )


def process_place(path, entry):
    client = get_client()
    title = entry.get("title")
    source_text = get_source_text_for_city(path, max_chars=MAX_SOURCE_CHARS)

    try:
        from_article = None
        if source_text:
            parsed = request_structured_json(
                client=client,
                schema=GEO_SCHEMA,
                schema_name="geocode_article",
                user_prompt=build_article_prompt(title, source_text),
                system_prompt=ARTICLE_SYSTEM_PROMPT,
                max_tokens=500,
                broken_response_tag=f"step2.article.{path}",
            )
            if parsed.get("found"):
                from_article = [parsed["lat"], parsed["lon"]]

        from_llm_knowledge = None
        if from_article is None:
            parsed = request_structured_json(
                client=client,
                schema=GEO_SCHEMA,
                schema_name="geocode_knowledge",
                user_prompt=build_knowledge_prompt(title),
                system_prompt=KNOWLEDGE_SYSTEM_PROMPT,
                max_tokens=500,
                broken_response_tag=f"step2.knowledge.{path}",
            )
            if parsed.get("found"):
                from_llm_knowledge = [parsed["lat"], parsed["lon"]]

        updated = {
            **entry,
            "path": path,
            "coords": {"from_article": from_article, "from_llm_knowledge": from_llm_knowledge},
        }
        return path, updated, None
    except Exception as e:
        return path, {**entry, "path": path,
                       "coords": {"from_article": None, "from_llm_knowledge": None},
                       "error": str(e)}, e


def rekey_by_coords(geocoded):
    """Re-keys each resolved city from its graph path to "{name}_{lat}_{lon}" (no merging needed)."""
    result = {}
    for entry in geocoded.values():
        resolved = resolve_dual(entry.get("coords"))
        if resolved is None:
            continue
        lat, lon = round(resolved[0], 5), round(resolved[1], 5)
        name = entry.get("title")
        key = f"{name}_{lat}_{lon}"
        result[key] = {
            "name": name,
            "alternative_names": [],
            "path": entry.get("path"),
            "coords": entry.get("coords"),
        }
    return result


def main():
    city_nodes = get_city_nodes()
    print(f"Loaded {len(city_nodes)} city nodes with text from the graph")

    geocoded = run_pool(
        items=city_nodes,
        worker=process_place,
        output_path=RAW_OUTPUT_PATH,
        max_workers=MAX_WORKERS,
        desc="step2-geocode",
    )

    skipped = {k: v for k, v in geocoded.items() if resolve_dual(v.get("coords")) is None}
    save_json(skipped, SKIPPED_OUTPUT_PATH)
    print(f"{len(skipped)} cities had neither an article nor a knowledge coordinate -> {SKIPPED_OUTPUT_PATH}")

    rekeyed = rekey_by_coords(geocoded)
    save_json(rekeyed, OUTPUT_PATH)
    print(f"{len(rekeyed)} geocoded cities re-keyed -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
