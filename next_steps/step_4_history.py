"""
Step 4 — extracts each city's chronological list of controlling entities, grounded in its own text plus connected subnodes. Text-only, no knowledge fallback.

Input:  OUTPUT_DIR/step_3_with_years.json
Output: OUTPUT_DIR/step_4_with_history.json
        OUTPUT_DIR/step_4_skipped_no_text.json  cities with no source text to ground on at all
"""
from common import (
    OUTPUT_DIR, get_client, request_structured_json, run_pool, resolve_dual, resolved_year_range,
    describe_coords, describe_year_range, get_source_text_for_city, load_required_json, save_json,
    JSON_VALIDITY_RULE,
)

from utils.period_merge import merge_and_split_periods

INPUT_PATH = OUTPUT_DIR / "step_3_with_years.json"
OUTPUT_PATH = OUTPUT_DIR / "step_4_with_history.json"
SKIPPED_OUTPUT_PATH = OUTPUT_DIR / "step_4_skipped_no_text.json"

MAX_WORKERS = 8
MAX_SOURCE_CHARS = 8000  # history needs more of the bundle than coords/years did

HISTORY_RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "start_year": {"type": "integer"},
        "start_month": {"type": "integer"},
        "start_day": {"type": "integer"},
        "end_year": {"type": "integer"},
        "end_month": {"type": "integer"},
        "end_day": {"type": "integer"},
        "entity": {"type": "string"},
        "tribe": {"type": "boolean"},
        "link": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": [
        "start_year", "start_month", "start_day", "end_year", "end_month", "end_day",
        "entity", "tribe", "link", "confidence",
    ],
}

HISTORY_SCHEMA = {
    "type": "object",
    "properties": {"history": {"type": "array", "items": HISTORY_RECORD_SCHEMA}},
    "required": ["history"],
}

SYSTEM_PROMPT = (
    "You are a neutral, independent historical assistant AI. Your role is to provide accurate, "
    "well-sourced, and context-rich historical information without taking sides, expressing "
    "opinions, or favoring any political, national, cultural, or ideological viewpoint."
)


def build_prompt(name, coords, start_year, end_year, source_text):
    return (
        f'Here is text about "{name}" {coords} — its own article, plus the text of every '
        f"country/region/tribe/culture article the graph connected it to:\n\n{source_text}\n\n"
        "Based ONLY on this text, extract the chronological list of independent geopolitical "
        f"owners (national country, empire, kingdom, tribal society, or archaeological culture of "
        f"highest authority — the latter for prehistoric places with no formal state) of this "
        f"place {describe_year_range(start_year, end_year)} — who the text says founded it, ruled "
        "it, captured it, was based there, etc. A related entity's own article describing its "
        "territorial extent/conquests counts as evidence here too, not just the main article.\n"
        "Small assumptions bridging an obvious gap between two facts the text already supports are "
        "fine (e.g. assuming the same entity still held it a few years later when nothing suggests "
        "a change of hands); never invent a whole new period, ruler, or entity that has no anchor "
        "in the text. If the text supports no ownership data at all, return an empty list. If the "
        "text gives a fact but never states which year(s) it applies to, do not invent a year for "
        "it — leave that entry out entirely rather than guessing a date.\n"
        "For each entity: \"tribe\" is true ONLY if it was a tribal society; if uncertain, use "
        "false. Names must be in English and as short as possible (e.g. \"Ottoman Empire\", not "
        "\"the state of the Ottoman Empire\"). If the immediate owner is a province/state inside a "
        "bigger geopolitical entity, use the name of the bigger entity instead. Use as few special "
        "characters as possible (e.g. 'o' instead of 'ó').\n"
        "\"link\" is a list of source URLs; leave it empty unless the text itself cites one — never "
        "invent a plausible-looking URL.\n"
        "\"confidence\" (0-1) reflects how directly the text supports each entry.\n"
        "Unknown start day/month -> 1 Jan. Unknown end day/month -> 31 Dec.\n"
        f"{JSON_VALIDITY_RULE}"
    )


def process_place(key, place):
    # Missing/unresolved coords or years no longer block extraction.
    start_year, end_year = resolved_year_range(place.get("years"))

    source_text = get_source_text_for_city(place["path"], max_chars=MAX_SOURCE_CHARS)
    if not source_text:
        return key, {**place, "history": [], "skip_reason": "no source text available"}, None

    client = get_client()
    name = place.get("name")
    coords = describe_coords(resolve_dual(place.get("coords")))

    try:
        parsed = request_structured_json(
            client=client,
            schema=HISTORY_SCHEMA,
            schema_name="history",
            user_prompt=build_prompt(name, coords, start_year, end_year, source_text),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=6000,
            broken_response_tag=f"step4.{key}",
        )
        history = merge_and_split_periods(parsed.get("history", []), "entity")
        return key, {**place, "history": history}, None
    except Exception as e:
        return key, {**place, "history": [], "error": str(e)}, e


def main():
    places = load_required_json(INPUT_PATH, label="step 3 output")
    print(f"Loaded {len(places)} cities from {INPUT_PATH}")

    results = run_pool(
        items=places,
        worker=process_place,
        output_path=OUTPUT_PATH,
        max_workers=MAX_WORKERS,
        desc="step4-history",
    )

    skipped = {k: v for k, v in results.items() if v.get("skip_reason")}
    save_json(skipped, SKIPPED_OUTPUT_PATH)
    if skipped:
        print(f"{len(skipped)} cities had no source text to ground on -> {SKIPPED_OUTPUT_PATH}")
    print(f"Done. {len(results)} cities with history -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
