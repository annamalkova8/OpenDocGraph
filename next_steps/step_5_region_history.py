"""
Step 5 — like step 4, but extracts the smallest sub-national administrative subdivision (oblast, province, duchy, etc.), never the sovereign country itself.

Input:  OUTPUT_DIR/step_4_with_history.json
Output: OUTPUT_DIR/step_5_with_region_history.json
        OUTPUT_DIR/step_5_skipped_no_text.json  cities with no source text to ground on at all
"""
from common import (
    OUTPUT_DIR, get_client, request_structured_json, run_pool, resolve_dual, resolved_year_range,
    describe_coords, describe_year_range, get_source_text_for_city, load_required_json, save_json,
    JSON_VALIDITY_RULE,
)

from utils.period_merge import merge_and_split_periods

INPUT_PATH = OUTPUT_DIR / "step_4_with_history.json"
OUTPUT_PATH = OUTPUT_DIR / "step_5_with_region_history.json"
SKIPPED_OUTPUT_PATH = OUTPUT_DIR / "step_5_skipped_no_text.json"

MAX_WORKERS = 8
MAX_SOURCE_CHARS = 8000

REGION_RECORD_SCHEMA = {
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

REGION_HISTORY_SCHEMA = {
    "type": "object",
    "properties": {"region_history": {"type": "array", "items": REGION_RECORD_SCHEMA}},
    "required": ["region_history"],
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
        "Based ONLY on this text, extract the chronological list of regional subdivisions "
        "(\"republic\", \"oblast\", \"autonomous region\", \"province\", \"principality\", "
        "\"duchy\", \"crown\", \"government\", \"order\", \"margraviate\", \"electorate\", "
        "\"confederation\", \"league\", \"lordship\", etc.) the text says this place belonged to, "
        f"{describe_year_range(start_year, end_year)}.\n"
        "Only include a subdivision if the text actually names it for that place — a connected "
        "'region' article describing itself as containing/administering this city counts as "
        "evidence here too, not just the main article. Small assumptions bridging an obvious gap "
        "between two facts the text already supports are fine; never invent a subdivision that has "
        "no anchor in the text. If the text names a subdivision but never states which year(s) it "
        "applies to, do not invent a year for it — leave that entry out entirely rather than "
        "guessing a date. Return the smallest administrative subdivision the text names — do NOT "
        "return the name of the sovereign country/empire itself, only the sub-national region.\n"
        "Use standardized, canonical, short English names for every entity (e.g. \"Ukrainian "
        "SSR\" not \"Ukrainian Soviet Socialist Republic\"; unify equivalent spelling variants; "
        "prefer the most widely accepted English historical name; no special characters, "
        "quotation marks, or brackets).\n"
        "\"tribe\" is true ONLY if it was a tribal society; if uncertain, use false. \"link\" is a "
        "list of source URLs; leave it empty unless the text itself cites one — never invent a "
        "plausible-looking URL. \"confidence\" (0-1) reflects how directly the text supports each "
        "entry.\n"
        "Unknown start day/month -> 1 Jan. Unknown end day/month -> 31 Dec.\n"
        "If the text never names a sub-national region for this place, return an empty list.\n"
        f"{JSON_VALIDITY_RULE}"
    )


def process_place(key, place):
    # Missing/unresolved coords or years no longer block extraction.
    start_year, end_year = resolved_year_range(place.get("years"))

    source_text = get_source_text_for_city(place["path"], max_chars=MAX_SOURCE_CHARS)
    if not source_text:
        return key, {**place, "region_history": [], "skip_reason": "no source text available"}, None

    client = get_client()
    name = place.get("name")
    coords = describe_coords(resolve_dual(place.get("coords")))

    try:
        parsed = request_structured_json(
            client=client,
            schema=REGION_HISTORY_SCHEMA,
            schema_name="region_history",
            user_prompt=build_prompt(name, coords, start_year, end_year, source_text),
            system_prompt=SYSTEM_PROMPT,
            max_tokens=6000,
            broken_response_tag=f"step5.{key}",
        )
        region_history = merge_and_split_periods(parsed.get("region_history", []), "entity")
        return key, {**place, "region_history": region_history}, None
    except Exception as e:
        return key, {**place, "region_history": [], "error": str(e)}, e


def main():
    places = load_required_json(INPUT_PATH, label="step 4 output")
    print(f"Loaded {len(places)} cities from {INPUT_PATH}")

    results = run_pool(
        items=places,
        worker=process_place,
        output_path=OUTPUT_PATH,
        max_workers=MAX_WORKERS,
        desc="step5-region-history",
    )

    skipped = {k: v for k, v in results.items() if v.get("skip_reason")}
    save_json(skipped, SKIPPED_OUTPUT_PATH)
    if skipped:
        print(f"{len(skipped)} cities had no source text to ground on -> {SKIPPED_OUTPUT_PATH}")
    print(f"Done. {len(results)} cities with region_history -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
