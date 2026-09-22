"""
Step 6 — settlement-size classification over time for each city, keeping text-grounded and
LLM-knowledge classifications separate (same pattern as step 2's coords and step 3's years).

Input:  OUTPUT_DIR/step_5_with_region_history.json
Output: OUTPUT_DIR/step_6_with_city_types.json   (feature-complete place record)
"""
from common import (
    OUTPUT_DIR, CITY_TYPES, get_client, request_structured_json, run_pool, resolve_dual,
    resolved_year_range, describe_coords, describe_year_range, get_source_text_for_city,
    load_required_json, save_json, JSON_VALIDITY_RULE,
)

from utils.period_merge import merge_and_split_periods

INPUT_PATH = OUTPUT_DIR / "step_5_with_region_history.json"
OUTPUT_PATH = OUTPUT_DIR / "step_6_with_city_types.json"

MAX_WORKERS = 8
MAX_SOURCE_CHARS = 6000

CITY_TYPE_RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "start_year": {"type": "integer"},
        "start_month": {"type": "integer"},
        "start_day": {"type": "integer"},
        "end_year": {"type": "integer"},
        "end_month": {"type": "integer"},
        "end_day": {"type": "integer"},
        "city_type": {"type": "string", "enum": CITY_TYPES},
        "confidence": {"type": "number"},
    },
    "required": [
        "start_year", "start_month", "start_day", "end_year", "end_month", "end_day",
        "city_type", "confidence",
    ],
}

CITY_TYPES_SCHEMA = {
    "type": "object",
    "properties": {"city_types": {"type": "array", "items": CITY_TYPE_RECORD_SCHEMA}},
    "required": ["city_types"],
}

SYSTEM_PROMPT = (
    "You are a neutral, independent historical assistant AI. Your role is to provide accurate, "
    "well-sourced, and context-rich historical information without taking sides, expressing "
    "opinions, or favoring any political, national, cultural, or ideological viewpoint."
)

CATEGORIES = (
    "1. large_city — one of the largest cities in its country/region or a national capital\n"
    "2. middle_city — medium city or regional/provincial capital\n"
    "3. small_city — small city, town, or village\n"
    "4. historical_site — archaeological/historical site, not a currently living populated place\n"
)


def describe_history(history):
    if not history:
        return ""
    spans = ", ".join(f"{h['entity']} ({h['start_year']} to {h['end_year']})" for h in history[:15])
    return f" It was historically controlled by: {spans}."


def build_article_prompt(name, coords, start_year, end_year, source_text, history):
    return (
        f'Here is text about "{name}" {coords} — its own article, plus any related '
        f"country/region/tribe/culture articles the graph connected it to:\n\n{source_text}\n\n"
        "Based ONLY on this text, classify the settlement into one of these categories for each "
        f"period of its existence, {describe_year_range(start_year, end_year)}:\n"
        f"{CATEGORIES}"
        f"{describe_history(history)}\n"
        "Return one entry per period during which the classification stayed the same (e.g. a "
        "small_city period followed by a middle_city period once it grew, etc.) — merge "
        "consecutive periods with the same classification into one entry rather than repeating "
        "it. Only include an entry when the text gives evidence of the settlement's size or "
        "importance in that period (population figures, \"capital of\", \"village\", \"one of the "
        "largest cities\", etc.); use \"confidence\" (0-1) to reflect how directly the text "
        "supports each entry. If the text gives no such evidence for any period, return an empty "
        "list — do NOT fall back on what you already know about a place with this name.\n"
        "Unknown start day/month -> 1 Jan. Unknown end day/month -> 31 Dec.\n"
        f"{JSON_VALIDITY_RULE}"
    )


def build_knowledge_prompt(name, coords, start_year, end_year, history):
    return (
        f'The source text gave no settlement-size evidence for "{name}" {coords}. Using your own '
        "historical knowledge, classify it into one of these categories for each period of its "
        f"existence, {describe_year_range(start_year, end_year)}:\n"
        f"{CATEGORIES}"
        f"{describe_history(history)}\n"
        "Return one entry per period during which the classification stayed the same — merge "
        "consecutive periods with the same classification into one entry rather than repeating "
        "it. Use \"confidence\" (0-1) to reflect how certain you are of each entry. If you have no "
        "reasonable idea for any period, return an empty list.\n"
        "Unknown start day/month -> 1 Jan. Unknown end day/month -> 31 Dec.\n"
        f"{JSON_VALIDITY_RULE}"
    )


def process_place(key, place):
    # Missing/unresolved coords or years no longer block extraction; the prompt gets an "unknown" placeholder.
    start_year, end_year = resolved_year_range(place.get("years"))
    source_text = get_source_text_for_city(place["path"], max_chars=MAX_SOURCE_CHARS)

    client = get_client()
    name = place.get("name")
    coords = describe_coords(resolve_dual(place.get("coords")))
    history = place.get("history", [])

    try:
        from_article = None
        if source_text:
            parsed = request_structured_json(
                client=client,
                schema=CITY_TYPES_SCHEMA,
                schema_name="city_types_article",
                user_prompt=build_article_prompt(name, coords, start_year, end_year, source_text, history),
                system_prompt=SYSTEM_PROMPT,
                max_tokens=4000,
                broken_response_tag=f"step6.article.{key}",
            )
            city_types = merge_and_split_periods(parsed.get("city_types", []), "city_type")
            if city_types:
                from_article = city_types

        from_llm_knowledge = None
        if from_article is None:
            parsed = request_structured_json(
                client=client,
                schema=CITY_TYPES_SCHEMA,
                schema_name="city_types_knowledge",
                user_prompt=build_knowledge_prompt(name, coords, start_year, end_year, history),
                system_prompt=SYSTEM_PROMPT,
                max_tokens=4000,
                broken_response_tag=f"step6.knowledge.{key}",
            )
            city_types = merge_and_split_periods(parsed.get("city_types", []), "city_type")
            if city_types:
                from_llm_knowledge = city_types

        updated = {
            **place,
            "city_types": {"from_article": from_article, "from_llm_knowledge": from_llm_knowledge},
            "population": place.get("population", []),
        }
        return key, updated, None
    except Exception as e:
        updated = {
            **place,
            "city_types": {"from_article": None, "from_llm_knowledge": None},
            "population": place.get("population", []),
            "error": str(e),
        }
        return key, updated, e


def main():
    places = load_required_json(INPUT_PATH, label="step 5 output")
    print(f"Loaded {len(places)} cities from {INPUT_PATH}")

    results = run_pool(
        items=places,
        worker=process_place,
        output_path=OUTPUT_PATH,
        max_workers=MAX_WORKERS,
        desc="step6-city-types",
    )

    neither = sum(1 for v in results.values() if resolve_dual(v.get("city_types")) is None)
    if neither:
        print(f"{neither} cities had neither article nor knowledge city types (kept, empty list)")
    print(f"Done. {len(results)} feature-complete place records -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
