"""Simple word-matching gazetteer, for any dataset with no structure to key off besides free text.
country_names.txt / region_names.txt are this project's own verified results (every node_type
"country"/"region" the wiki sample actually classified) — reusable by anyone who wants a quick way
to spot place mentions in arbitrary text, no per-format parsing required.
"""
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_NAMES_DIR = _HERE / "sources"


def _load_names(path):
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _build_pattern(names):
    if not names:
        return None
    ordered = sorted(names, key=len, reverse=True)  # longest first: "United Kingdom" before "..."
    return re.compile(r"\b(" + "|".join(re.escape(n) for n in ordered) + r")\b")


COUNTRY_NAMES = _load_names(_NAMES_DIR / "country_names.txt")
REGION_NAMES = _load_names(_NAMES_DIR / "region_names.txt")
_COUNTRY_RE = _build_pattern(COUNTRY_NAMES)
_REGION_RE = _build_pattern(REGION_NAMES)


def find_countries(text):
    if not text or _COUNTRY_RE is None:
        return []
    return sorted(set(_COUNTRY_RE.findall(text)))


def find_regions(text):
    if not text or _REGION_RE is None:
        return []
    return sorted(set(_REGION_RE.findall(text)))


def find_all(text):
    return {"countries": find_countries(text), "regions": find_regions(text)}


if __name__ == "__main__":
    import json

    out_path = _HERE / "output" / "universal_graph_europeana_1200.json"
    if not out_path.exists():
        print(f"{out_path} doesn't exist — run graph_extractor_universal.py with "
              f"CONFIG = EUROPEANA_1200_CONFIG first.")
    else:
        data = json.loads(out_path.read_text(encoding="utf-8"))
        hits = 0
        for node in data["nodes"]:
            text = " ".join([node.get("title") or ""] + (node.get("extra", {}).get("creators") or []))
            matches = find_all(text)
            if matches["countries"] or matches["regions"]:
                hits += 1
                print(f"{node['path']}: {matches}")
        print(f"\n{hits}/{len(data['nodes'])} nodes matched a known country/region name.")
