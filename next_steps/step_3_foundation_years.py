"""
Step 3 — extracts foundation/last-attested years per city, dual-tracked as text-grounded vs LLM-knowledge.

Input:  OUTPUT_DIR/step_2_geocoded.json
Output: OUTPUT_DIR/step_3_with_years.json
        OUTPUT_DIR/step_3_skipped_no_years.json   cities with neither article nor knowledge years
"""
import datetime

from common import (
    OUTPUT_DIR, get_client, request_structured_json, run_pool, resolve_dual, describe_coords,
    get_source_text_for_city, load_required_json, save_json,
)

INPUT_PATH = OUTPUT_DIR / "step_2_geocoded.json"
OUTPUT_PATH = OUTPUT_DIR / "step_3_with_years.json"
SKIPPED_OUTPUT_PATH = OUTPUT_DIR / "step_3_skipped_no_years.json"

MAX_WORKERS = 8
MAX_SOURCE_CHARS = 4000
CURRENT_YEAR = datetime.date.today().year

YEARS_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "foundation_year": {"type": "integer"},
        "end_year": {"type": "integer"},
        "still_exists": {"type": "boolean"},
    },
    "required": ["found", "foundation_year", "end_year", "still_exists"],
}

ARTICLE_SYSTEM_PROMPT = (
    "You are a strict, source-grounded historical data API. Use negative integers for BCE years. "
    "You only report years the given text actually states or clearly implies; you never recall a "
    "year from outside knowledge the text doesn't support."
)

KNOWLEDGE_SYSTEM_PROMPT = (
    "You are a strict historical data API. Use negative integers for BCE years. Answer with your "
    "best estimate from your own knowledge; set found=false only if you have no reasonable idea."
)


def build_article_prompt(name, coords, source_text):
    return (
        f'Here is text about the settlement "{name}" located at {coords} — its own article, plus '
        f"any related country/region/tribe/culture articles the graph connected it to:\n\n"
        f"{source_text}\n\n"
        "Based ONLY on this text: in what year was it first mentioned in historical/archaeological "
        "records or founded? In what year was it last attested, or does it still exist today?\n"
        "An explicit or clearly-implied date phrase counts as grounded (a literal year; \"founded in "
        "the 18th century\" -> use 1750; \"a Roman-era settlement\" -> use a reasonable Roman-era "
        f"year; the text describing it in the present tense as an existing place -> end_year={CURRENT_YEAR}, "
        "still_exists=true).\n"
        "Set \"found\" to true only if the text supports both a start and an end this way. Set it to "
        "false (and leave the other fields at 0/false) if the text gives no usable date information "
        "for founding or persistence — do NOT fall back on what you already know about a place with "
        "this name.\n"
        "Use negative integers for years BCE (e.g. 500 BC -> -500)."
    )


def build_knowledge_prompt(name, coords):
    return (
        f'The source text gave no usable dates for the settlement "{name}" located at {coords}. '
        "From your own knowledge: in what year was it first mentioned in historical/archaeological "
        "records or founded? In what year was it last attested, or does it still exist today (if so, "
        f"still_exists=true, end_year={CURRENT_YEAR})?\n"
        "Use negative integers for years BCE (e.g. 500 BC -> -500). Set \"found\" to false only if "
        "you have no reasonable idea."
    )


def process_place(key, place):
    # Missing coordinate no longer blocks extraction; just show "unknown".
    coords_display = describe_coords(resolve_dual(place.get("coords")))

    source_text = get_source_text_for_city(place["path"], max_chars=MAX_SOURCE_CHARS)
    client = get_client()
    name = place.get("name")

    try:
        from_article = None
        if source_text:
            parsed = request_structured_json(
                client=client,
                schema=YEARS_SCHEMA,
                schema_name="years_article",
                user_prompt=build_article_prompt(name, coords_display, source_text),
                system_prompt=ARTICLE_SYSTEM_PROMPT,
                max_tokens=600,
                broken_response_tag=f"step3.article.{key}",
            )
            if parsed.get("found"):
                from_article = {"start": parsed["foundation_year"], "end": parsed["end_year"]}

        from_llm_knowledge = None
        if from_article is None:
            parsed = request_structured_json(
                client=client,
                schema=YEARS_SCHEMA,
                schema_name="years_knowledge",
                user_prompt=build_knowledge_prompt(name, coords_display),
                system_prompt=KNOWLEDGE_SYSTEM_PROMPT,
                max_tokens=600,
                broken_response_tag=f"step3.knowledge.{key}",
            )
            if parsed.get("found"):
                from_llm_knowledge = {"start": parsed["foundation_year"], "end": parsed["end_year"]}

        updated = {**place, "years": {"from_article": from_article, "from_llm_knowledge": from_llm_knowledge}}
        return key, updated, None
    except Exception as e:
        return key, {**place, "years": {"from_article": None, "from_llm_knowledge": None}, "error": str(e)}, e


def main():
    places = load_required_json(INPUT_PATH, label="step 2 output")
    print(f"Loaded {len(places)} geocoded cities from {INPUT_PATH}")

    results = run_pool(
        items=places,
        worker=process_place,
        output_path=OUTPUT_PATH,
        max_workers=MAX_WORKERS,
        desc="step3-foundation-years",
    )

    skipped = {k: v for k, v in results.items() if resolve_dual(v.get("years")) is None}
    save_json(skipped, SKIPPED_OUTPUT_PATH)
    print(f"{len(skipped)} cities had neither article nor knowledge years -> {SKIPPED_OUTPUT_PATH}")
    print(f"Done. {len(results) - len(skipped)} cities with resolvable years -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
