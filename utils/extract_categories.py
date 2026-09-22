from bs4 import BeautifulSoup
from urllib.parse import parse_qs, unquote, urlparse


def _normalize_category_name(raw_value):
    if not raw_value:
        return None

    category = unquote(str(raw_value)).strip()
    if not category:
        return None

    category = category.split("#", 1)[0]
    category = category.replace("_", " ").strip()

    lowered = category.lower()
    if lowered.startswith("category:"):
        category = category.split(":", 1)[1].strip()

    return category or None


def _extract_category_from_href(href):
    if not href:
        return None

    parsed = urlparse(href)
    path = unquote(parsed.path or "")

    if "Category:" in path:
        category = path.split("Category:", 1)[1]
        return _normalize_category_name(category)

    title_values = parse_qs(parsed.query).get("title", [])
    for title in title_values:
        if title.startswith("Category:"):
            return _normalize_category_name(title)

    if href.startswith("Category:"):
        return _normalize_category_name(href)

    return None


def _append_category(categories, seen, value):
    normalized = _normalize_category_name(value)
    if not normalized:
        return

    category_key = normalized.casefold()
    if category_key in seen:
        return

    seen.add(category_key)
    categories.append(normalized)


def extract_all_categories(html):
    soup = BeautifulSoup(html, "lxml")

    categories = []
    seen = set()

    category_blocks = []
    for block_id in ["mw-normal-catlinks", "mw-hidden-catlinks"]:
        block = soup.find(id=block_id)
        if block:
            category_blocks.append(block)

    category_blocks.extend(soup.select("#catlinks, .catlinks, [class*='catlinks']"))

    for block in category_blocks:
        for element in block.find_all(attrs={"data-mw-category": True}):
            _append_category(categories, seen, element.get("data-mw-category"))

        for link in block.find_all("a", href=True):
            category_from_href = _extract_category_from_href(link.get("href"))
            if category_from_href:
                _append_category(categories, seen, category_from_href)
                continue

            link_text = link.get_text(" ", strip=True)
            if link_text and link_text.lower().startswith("category:"):
                _append_category(categories, seen, link_text)

    for element in soup.find_all(attrs={"data-category": True}):
        _append_category(categories, seen, element.get("data-category"))

    for meta in soup.find_all("meta"):
        for attr_name in ("content", "property", "name"):
            attr_value = meta.get(attr_name)
            if not attr_value or "Category:" not in attr_value:
                continue

            for part in attr_value.split("|"):
                if "Category:" in part:
                    _append_category(categories, seen, part.split("Category:", 1)[1])

    for link in soup.find_all("a", href=True):
        category_from_href = _extract_category_from_href(link.get("href"))
        if category_from_href:
            _append_category(categories, seen, category_from_href)

    return categories