from bs4 import BeautifulSoup


def extract_links_proper(html):
    """Real found-it-the-hard-way gotcha: without stripping navbox elements first, a country
    article's "Sovereign states of Europe"-style navbox (or any other membership-list template —
    G7, G20, EU members, etc.) leaks in as if every other country listed there were a genuine
    in-article link — e.g. France and Italy end up "linked" purely because a shared navbox lists
    both, not because either article's actual prose mentions the other. utils/geohistorical_extractor.py's
    extract_page_text() already strips navbox for the same reason (its own docstring: "leak sibling-
    article lists... into the page's own text"); this function does the identical thing for links,
    which it was missing before."""
    soup = BeautifulSoup(html, "lxml")

    # Use only the main article content
    content = soup.find("div", {"id": "mw-content-text"})
    if not content:
        content = soup

    for navbox in content.find_all(attrs={"class": lambda c: c and "navbox" in c}):
        navbox.decompose()

    # Second gotcha, found the same way: not every enumeration-list problem is a navbox. A major
    # capital's "list of countries with diplomatic relations" section (bare flag icon + country
    # name, no other text, one per <li>) isn't a navbox template but has the identical effect —
    # China has relations with nearly every UN member, so Beijing "linking" to ~175 countries this
    # way carries zero real signal. Narrow on purpose: only strips an <li> whose only content is a
    # flag icon plus a single link with no other text, so it doesn't touch list items that do carry
    # real per-entry text (e.g. a sister-cities list giving "Rome, Italy" or similar).
    for li in content.find_all("li"):
        if not li.find("span", class_="flagicon"):
            continue
        links_in_li = li.find_all("a")
        link_text_len = sum(len(a.get_text(strip=True)) for a in links_in_li)
        if len(links_in_li) <= 1 and len(li.get_text(strip=True)) <= link_text_len + 5:
            li.decompose()

    links = set()

    for a in content.find_all("a", href=True):
        href = a["href"]

        # skip external links
        if href.startswith("http"):
            continue

        # normalise
        href = href.split("#")[0]
        href = href.replace("../", "").strip()

        # filter junk
        if not href:
            continue

        if ":" in href:
            continue

        if href.startswith("_"):
            continue

        links.add(href)

    return list(links)