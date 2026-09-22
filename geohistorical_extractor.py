"""
Builds a city-centered link-graph of geo/entity Wikipedia articles from the .zim archive
(replacement for __step_prolog_wiki.py's seed lists). Full design: READ_graph_bd.md.

  Phase 1 (classify) — node_type (city/country/region/tribe/culture) via infobox + lead-keyword +
    coords signals, no text extraction yet. Also captures lat/lon and a best-effort founding year
    (see extract_coords/extract_founding_year).
  Phase 2 (extract) — pulls full text + hyperlink edges (edge_source='link'); city-to-city links
    are excluded (near-always editorial/etymological, not a real subnode relationship).
  Phase 3 (link) — connects entities to cities by name cross-reference (edge_source='name_match').
  Disambiguator-linking pass — matches a city's disambiguator suffix to an in-graph non-city node
    (edge_source='disambiguator_match'); runs after Phase 3.

Resumable, single writer thread (SQLite conns aren't thread-shareable). Standalone/long-running,
not wired into steps 1-7 yet; tune PHASE1_ENTRY_LIMIT down for a smoke test.
"""
import json
import queue
import re
import sqlite3
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import ahocorasick
import libzim
from bs4 import BeautifulSoup

# FUND_restack is self-contained: utils/ and names_search.py both live directly in this folder, so
# the only sys.path entry ever needed is this folder itself (added so these imports resolve
# regardless of the current working directory or how this module was reached).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils.extract_links import extract_links_proper  # noqa: E402

# Fuzzy+embedding NameMatcher the disambiguator-linking pass uses; imported directly (not via
# common.py) to avoid common.py's unrelated vLLM-client/dotenv side effects.
from names_search import NameMatcher, STATE_NOISE_TERMS  # noqa: E402

# "zim" = ZIM_PATH (needs libzim + the 124GB file). "sample" = data/wiki_sample/, no download needed.
SOURCE_FORMAT = "sample"

ZIM_PATH = "/Volumes/T7/AI_HISTORICA/Archive/anna/DEV/wikipedia_en_all_maxi_2026-02.zim"
SAMPLE_DIR = Path(__file__).resolve().parent / "data" / "wiki_sample"

# Not on the .zim's ExFAT drive — ExFAT's POSIX locking is flaky on macOS and caused persistent
# "readonly database" errors there; APFS is reliable. Reading the .zim from ExFAT is fine.
DB_PATH = Path.home() / "graph_extractor_data" / "graph.sqlite3"

MAX_WORKERS = 24  # ZIM reads + regex are the bottleneck per worker, not the writer thread
WRITE_BATCH_SIZE = 500  # commit every N queued writes

BAD_PREFIXES = ("File:", "Category:", "Template:", "Wikipedia:", "Portal:", "Help:", "Special:", "_")

# Set to a small int (e.g. 200_000) for a quick smoke test; None = scan the whole archive.
PHASE1_ENTRY_LIMIT = None

# Run the disambiguator-linking pass (see link_disambiguators) right after Phase 3.
RUN_DISAMBIGUATOR_LINK_PASS = True

# Own cache — different corpus (this graph's own non-city node titles, not the main dataset's).
DISAMBIGUATOR_MATCHER_CACHE_PATH = Path(__file__).resolve().parent / "output" / "name_matcher_cache" / "disambiguator_titles.pkl"
DISAMBIGUATOR_MATCH_MIN_SCORE = 0.85  # higher than NAME_CLASSIFIER_MIN_SCORE — a wrong edge here pollutes prompt grounding, not just node_type


# SampleArchive: minimal libzim.Archive-compatible shim over data/wiki_sample/. See READ_graph_bd.md.
class _SampleItem:
    def __init__(self, content_bytes):
        self.content = content_bytes
        self.mimetype = "text/html"


class _SampleEntry:
    def __init__(self, path, title, html_bytes):
        self.path = path
        self.title = title
        self.is_redirect = False
        self._html_bytes = html_bytes

    def get_item(self):
        return _SampleItem(self._html_bytes)


class SampleArchive:
    def __init__(self, sample_dir):
        sample_dir = Path(sample_dir)
        manifest = json.loads((sample_dir / "manifest.json").read_text(encoding="utf-8"))
        self._entries = []
        self._by_path = {}
        for fname, meta in sorted(manifest.items()):
            html_bytes = (sample_dir / fname).read_bytes()
            entry = _SampleEntry(meta["zim_path"], meta["title"], html_bytes)
            self._entries.append(entry)
            self._by_path[meta["zim_path"]] = entry

    @property
    def all_entry_count(self):
        return len(self._entries)

    @property
    def article_count(self):
        return len(self._entries)

    def _get_entry_by_id(self, entry_id):
        return self._entries[entry_id]

    def get_entry_by_path(self, path):
        return self._by_path[path]


def open_archive():
    if SOURCE_FORMAT == "sample":
        return SampleArchive(SAMPLE_DIR)
    return libzim.Archive(ZIM_PATH)

# How much raw HTML the coords/keyword signals look at — generous because infobox markup + CSS
# can run past 15,000 chars before the real lead sentence appears.
LEAD_HTML_CHARS = 20000
LEAD_TEXT_CHARS = 2000  # tag-stripped lead text actually searched for keyword phrases

# Strips <script>/<style> blocks (not prose) and then every remaining tag.
_TAG_STRIP_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.DOTALL)

_CONTENT_DIV_MARKER = 'id="mw-content-text"'


def _lead_text(html_str):
    content_start = html_str.find(_CONTENT_DIV_MARKER)
    if content_start != -1:
        html_str = html_str[content_start:]
    stripped = _TAG_STRIP_RE.sub(" ", html_str[:LEAD_HTML_CHARS])
    return " ".join(stripped.split())[:LEAD_TEXT_CHARS]

# --- Signal 1: infobox template family -> node type (strongest signal) --------------------------
NODE_TYPE_INFOBOX_TYPES = {
    "settlement": "city",
    "uk-place": "city",
    "country": "country",
    "former-country": "country",
    "former-subdiv": "region",
    "pol-div": "region",
    "islands": "region",
    "landform": "region",
    "ethnic-group": "tribe",
}

_IB_TABLE_RE = re.compile(r'<table class="([^"]*infobox[^"]*)"')
_IB_NAME_RE = re.compile(r"\bib-([a-z0-9-]+)")

_BIOGRAPHY_INFOBOX_RE = re.compile(r"\bbiography\b")


def _is_biography_infobox(infobox_class):
    return bool(_BIOGRAPHY_INFOBOX_RE.search(infobox_class))


_COUNTY_TITLE_RE = re.compile(r"\bcounty\b", re.IGNORECASE)


def _title_is_county(title):
    core, _ = split_title_disambiguation(title)
    return bool(_COUNTY_TITLE_RE.search(core or ""))

_DISTRICT_TITLE_RE = re.compile(r"\bdistrict\b", re.IGNORECASE)
_HISTORIC_DISTRICT_RE = re.compile(r"\bhistoric\s+district\b", re.IGNORECASE)


def _title_is_district(title):
    core, _ = split_title_disambiguation(title)
    core = core or ""
    return bool(_DISTRICT_TITLE_RE.search(core)) and not _HISTORIC_DISTRICT_RE.search(core)


# --- Signal 2: lead-sentence phrasing -> node type (2nd strongest; only real signal for tribe/culture) --
def _phrase_pattern(words):
    # "is one of(?: the)?" catches plural leads like "...is one of the governorates of Yemen".
    alternation = "|".join(re.escape(w) for w in words)
    return re.compile(
        r"\b(?:is an?|is one of(?: the)?)\s+(?:small |large |medium-sized |former |historic |"
        r"ancient |ruined |coastal |landlocked |sovereign |former sovereign |prehistoric |"
        r"Neolithic |Bronze Age |Iron Age )*"
        r"(?:" + alternation + r")s?\b",
        re.IGNORECASE,
    )

CATEGORY_PHRASE_PATTERNS = {
    "city": _phrase_pattern([
        "city", "town", "village", "municipality", "hamlet", "settlement", "county seat", "capital",
    ]),
    "country": _phrase_pattern([
        "country", "sovereign state", "kingdom", "empire", "nation", "republic", "principality",
        "duchy", "sultanate", "caliphate", "khanate",
    ]),

    "region": _phrase_pattern([
        "region", "province", "county", "oblast", 
        "prefecture", "canton", "territory",
        "chiefdom", "governorate","district",
        "krai", "okrug", "department", "borough",
        "parish", "state", "division","voivodeship", 
    ]),
    "tribe": _phrase_pattern([
        "tribe", "tribal confederation", "tribal people", "ethnic group", "clan"
    ]),

    "culture": _phrase_pattern([
        "archaeological culture", "material culture", "prehistoric culture",
    ]),
}


# --- Signal 3: geo-coordinates present, nothing else matched (weakest — fallback only) ----------
def _has_geo_coords(lead_html):
    return 'class="geo' in lead_html


NON_SETTLEMENT_TITLE_KEYWORDS = [
    "university", "college", "institute", "academy", "school", "gymnasium", "kindergarten",
    "museum", "library", "gallery", "theatre", "theater", "auditorium", "studio",
    "market", "bazaar", "mall",
    "square", "plaza", "park", "geopark", "zoo",
    "monument", "memorial", "statue", "obelisk", "cairn",
    "stadium", "arena", "airport", "station", "amphitheater", "amphitheatre", "colosseum",
    "ministry", "parliament", "government", "embassy", "consulate", "congress", "senate",
    "courthouse", "capitol",
    "festival", "carnival", "holiday",
    "war", "battle", "treaty", "uprising", "revolution", "massacre", "siege", "disaster",
    "church", "cathedral", "basilica", "chapel", "monastery", "temple", "mosque", "synagogue",
    "shrine", "pagoda", "stupa", "gurdwara",
    "palace",
    "spring","mausoleum","goblet",
    "Management","Inspection","House","Company",
    "tomb","cemetery","graveyard","crypt","catacomb","necropolis","ruins", "historic district",
    "Bridge","Railway",
    "hall", "castle", "building", "hospital", "clinic", "prison", "asylum", "apartments",
    "opera", "cinema", "casino", "hotel", "resort", "spa", "club", "golf",
    "diner", "bistro",
    "farm", "ranch", "plantation", "manor", "mansion", "abbey", "seminary",
    "tower", "gate", "wall", "tunnel", "pier", "wharf", "lighthouse", "arch",
    "dam", "reservoir", "canal", "aqueduct", "well", "mine", "quarry",
    "mill", "factory", "refinery", "foundry", "workshop", "warehouse",
    "sawmill", "plant",
    "shipyard", "dockyard", "drydock", "armory", "arsenal", "barracks", "garrison",
    "depot", "terminal", "spaceport", "headquarters",
    "bureau", "authority", "commission", "agency", "office", "network", "hub",
    "microformat", "geocaching",
    "center", "centre", "complex", "site", "battlefield", "battleground", "circuit", "track",
    "crater", "glacier", "volcano", "waterfall", "cave", "caves", "grotto", "sinkhole",
    "canyon", "gorge", "ridge", "peninsula", "cape", "channel", "strait", "isthmus",
    "archipelago", "reef", "estuary", "delta", "basin", "plateau", "summit", "dune",
    "cliff", "cliffs", "falls", "rapids", "geyser", "oasis",
    "wildlife", "sanctuary", "reserve", "forest", "wilderness", "garden", "gardens", "fountain",
    "List of ",
    "River", "Lake", "Mountain", "Mount", "Hill", "Valley", "Bay", "Beach", "Coast", "Coastal",
    "Windmill", "fortress",
    "sea", "ocean", "gulf", "sound", "lagoon", "harbor", "harbour",
    "tree","township",
    "battery", "restaurant", "cafe", "café", "bakery", "brewpub", "brewery", "distillery", "winery",
    "observatory", "home","mound","chambers",
    "volcanic group"
    ]
_NON_SETTLEMENT_TITLE_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in NON_SETTLEMENT_TITLE_KEYWORDS) + r")\b", re.IGNORECASE
)


def _title_is_non_settlement(title):
    """True if title contains a whole-word institution/landmark/event keyword (word-boundary, so "church" doesn't match "Christchurch")."""
    return bool(_NON_SETTLEMENT_TITLE_RE.search(title or ""))


def extract_page_text(html_str):
    """Same extraction __step_prolog_wiki.py uses, kept local to avoid its module-level side effects. Strips navbox elements first — they otherwise leak sibling-article lists (e.g. every commune in a department) into the page's own text."""
    soup = BeautifulSoup(html_str, "lxml")
    content = soup.find("div", {"id": "mw-content-text"})
    if not content:
        content = soup
    for navbox in content.find_all(attrs={"class": lambda c: c and "navbox" in c}):
        navbox.decompose()
    return " ".join(content.get_text(" ", strip=True).split())


# Normalizes apostrophe-like glyph variants (e.g. Arabic-transliteration ʽ vs plain ') so title/text substring checks don't spuriously miss real matches.
_APOSTROPHE_VARIANTS_RE = re.compile("[‘’ʻʼʽ´`]")


def _normalize_apostrophes(text):
    return _APOSTROPHE_VARIANTS_RE.sub("'", text)


def _title_precedes_match(lead_text, match_start, title, window=80):
    """Requires the article's own title shortly before an "is a X" match, so a passing mention of some other entity (e.g. "Paraguay is a landlocked country" in a sports article) isn't misclassified as this article's subject."""
    if not title:
        return True  # no title given, can't check — permissive
    prefix = _normalize_apostrophes(lead_text[max(0, match_start - window):match_start].lower())
    return _normalize_apostrophes(title.lower()) in prefix


# Wikipedia's "Foo of Bar" sub-article convention (Geography of Laos, History of Abkhazia, ...) — always about a place, never a node type itself; excluded before any signal runs since their lead prose often restates the parent's own classification.
_META_TOPIC_ARTICLE_RE = re.compile(
    r"^(?:geography|history|economy|economics|demographics|demography|politics|culture|climate|"
    r"transport|transportation|military|foreign relations|government|religion|education|health|"
    r"healthcare|sports?|tourism|wildlife|geology|environment|administrative divisions|elections|"
    r"languages?|media|law|crime|agriculture|industry|energy|telecommunications|communications|"
    r"cuisine|architecture)\s+(?:of|in)\s",
    re.IGNORECASE,
)


def _is_meta_topic_article(title):
    return bool(_META_TOPIC_ARTICLE_RE.match(title or ""))


def classify_node(html_str, title=None):
    """Combines the three signals above, strongest first. Returns (node_type, source), source being "infobox"|"keyword"|"coords", or (None, None) if not a node."""
    if _is_meta_topic_article(title):
        return None, None

    m = _IB_TABLE_RE.search(html_str)
    if m:
        ib_match = _IB_NAME_RE.search(m.group(1))
        if ib_match:
            node_type = NODE_TYPE_INFOBOX_TYPES.get(ib_match.group(1))
            if node_type:
                if node_type == "city" and (_title_is_county(title) or _title_is_district(title)):
                    return "region", "infobox"
                return node_type, "infobox"
        if _is_biography_infobox(m.group(1)):
            # A person's coordinate (birthplace, workplace...) isn't the article's own subject — reject outright.
            return None, None

    lead_text = _lead_text(html_str)
    best_type, best_pos = None, None
    for node_type, pattern in CATEGORY_PHRASE_PATTERNS.items():
        for match in pattern.finditer(lead_text):
            if not _title_precedes_match(lead_text, match.start(), title):
                continue
            if best_pos is None or match.start() < best_pos:
                best_type, best_pos = node_type, match.start()
            break
    if best_type:
        return best_type, "keyword"

    if _has_geo_coords(html_str[:LEAD_HTML_CHARS]):
        if _title_is_non_settlement(title):
            # Geo-tagged but the title names an institution/landmark/event, not a settlement.
            return None, None
        # Weakest tier — see READ_graph_bd.md; filter on classification_source='coords' to exclude.
        return "city", "coords"

    return None, None


# --- Geo/historical extraction: coordinates + founding year, run once a node classifies ---------
# Both regexes were validated against real .zim entries before being wired in here (Bologna,
# France, Yorkshire, New_York_City, Detroit, Rome, Istanbul — see the conversation this was built
# in for the spot-check, or just re-run it if the .zim's markup ever changes).
_GEO_HREF_RE = re.compile(r'href="geo:(-?[\d.]+),(-?[\d.]+)"')

_INFOBOX_ROW_RE = re.compile(
    r'class="infobox-label"[^>]*>(.*?)</th>\s*<td[^>]*class="infobox-data"[^>]*>(.*?)</td>',
    re.DOTALL,
)
_FOUNDING_KEYWORD_RE = re.compile(
    r"\b(founded|established|settled|incorporated|formation)\b", re.IGNORECASE
)
_EXCLUDE_LABEL_RE = re.compile(r"\bby\b", re.IGNORECASE)
_TAG_STRIP_INLINE_RE = re.compile(r"<[^>]+>")
_BDAY_RE = re.compile(r'class="[^"]*\bbday\b[^"]*"[^>]*>([^<]+)<')
_ISO_DATE_RE = re.compile(r"\b(\d{3,4})-(\d{2})-(\d{2})\b")
_YEAR_TOKEN_RE = re.compile(r"(\d{3,4})\s*(BCE|BC)?", re.IGNORECASE)


def extract_coords(html_str):
    """Returns (lat, lon) floats from the first Coord-template geo: link, or (None, None)."""
    m = _GEO_HREF_RE.search(html_str)
    if not m:
        return None, None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None, None


def _year_from_text(text):
    matches = list(_YEAR_TOKEN_RE.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    year = int(m.group(1))
    return -year if m.group(2) else year


def extract_founding_year(html_str):
    """Best-effort: returns (year, 'infobox_regex') from the earliest-dated founding-type infobox
    row (Founded/Established/Settled/Incorporated/Formation, excluding "...by" rows which name a
    person), or (None, None) if no such row is present — most articles simply don't have one."""
    candidates = []
    for m in _INFOBOX_ROW_RE.finditer(html_str):
        label_raw, val_raw = m.group(1), m.group(2)
        label = " ".join(_TAG_STRIP_INLINE_RE.sub(" ", label_raw).split())
        if not _FOUNDING_KEYWORD_RE.search(label) or _EXCLUDE_LABEL_RE.search(label):
            continue
        bday = _BDAY_RE.search(val_raw)
        iso = _ISO_DATE_RE.search(val_raw)
        if bday:
            year = _year_from_text(bday.group(1))
        elif iso:
            year = int(iso.group(1))
        else:
            val_text = " ".join(_TAG_STRIP_INLINE_RE.sub(" ", val_raw).split())
            year = _year_from_text(val_text)
        if year is not None:
            candidates.append(year)
    if not candidates:
        return None, None
    return min(candidates), "infobox_regex"


# ---------------------------------------------------------------------------
# Phase 3 — connect entities to cities by name cross-reference, on top of Phase 2's hyperlink
# edges. Aho-Corasick scans each document for the whole name dictionary in one linear pass,
# regardless of dictionary size — a naive nested loop couldn't do this at this scale.
# ---------------------------------------------------------------------------
MIN_NAME_LENGTH_FOR_MATCH = 4  # skip very short titles ("Man", "Aar") — false substring matches get common below this

_WORD_CHAR_RE = re.compile(r"[A-Za-z0-9]")


def build_automaton(path_title_pairs):
    """Builds a case-insensitive automaton from (path, title) pairs, skipping short titles."""
    automaton = ahocorasick.Automaton()
    for path, title in path_title_pairs:
        if not title or len(title) < MIN_NAME_LENGTH_FOR_MATCH:
            continue
        automaton.add_word(title.lower(), (path, title))
    automaton.make_automaton()
    return automaton


def find_name_matches(automaton, text):
    """Yields each matching path once, whole-word-ish (rejects alphanumeric neighbors). len()==0 guards an empty automaton, since .iter() raises on one built from zero words rather than just finding nothing."""
    if len(automaton) == 0:
        return
    haystack = text.lower()
    seen = set()
    for end_index, (path, title) in automaton.iter(haystack):
        if path in seen:
            continue
        start_index = end_index - len(title) + 1
        before_ok = start_index == 0 or not _WORD_CHAR_RE.match(haystack[start_index - 1])
        after_ok = end_index + 1 >= len(haystack) or not _WORD_CHAR_RE.match(haystack[end_index + 1])
        if before_ok and after_ok:
            seen.add(path)
            yield path


# SQLite's default busy_timeout is 0 (raises immediately on lock contention); 30s gives it room to recover from ExFAT's flaky locking instead of hard-failing.
SQLITE_BUSY_TIMEOUT_MS = 30_000


def _configure_connection(conn):
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")


def open_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: writes actually happen on DBWriter's thread, serialized via its queue.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _configure_connection(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nodes (
            path TEXT PRIMARY KEY,
            title TEXT,
            node_type TEXT,
            classification_source TEXT,  -- 'infobox' | 'keyword' | 'coords'
            lat REAL,
            lon REAL,
            date_start INTEGER,  -- founding year, astronomical numbering (BCE negative); often NULL
            date_end INTEGER,    -- always equals date_start here (no ranges extracted from wiki)
            date_source TEXT,    -- 'infobox_regex', or NULL if no founding-date row was found
            extra_json TEXT,     -- unused for this source; present for schema parity with graph_export.py
            text_compressed BLOB,
            text_length INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);

        CREATE TABLE IF NOT EXISTS edges (
            source_path TEXT,
            target_path TEXT,
            edge_source TEXT,   -- 'link' (literal hyperlink, Phase 2, never city-to-city) | 'name_match' (Phase 3)
            PRIMARY KEY (source_path, target_path, edge_source)
        );
        CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_path);
        CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_path);

        CREATE TABLE IF NOT EXISTS scan_progress (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_entry_id INTEGER NOT NULL,
            phase TEXT NOT NULL
        );
        """
    )
    # CREATE TABLE IF NOT EXISTS above is a no-op against a nodes table from a run before
    # lat/lon/date_*/extra_json existed — add them by hand so an existing production db (e.g.
    # ~/graph_extractor_data/graph.sqlite3 from a prior run) doesn't break on the new INSERT below.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}
    for col, decl in [
        ("lat", "REAL"), ("lon", "REAL"), ("date_start", "INTEGER"),
        ("date_end", "INTEGER"), ("date_source", "TEXT"), ("extra_json", "TEXT"),
    ]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {decl}")
    conn.commit()
    return conn


def get_progress(conn):
    row = conn.execute("SELECT last_entry_id, phase FROM scan_progress WHERE id = 1").fetchone()
    return row if row else (-1, "classify")


def set_progress(conn, last_entry_id, phase):
    conn.execute(
        "INSERT INTO scan_progress (id, last_entry_id, phase) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET last_entry_id = excluded.last_entry_id, phase = excluded.phase",
        (last_entry_id, phase),
    )


# ---------------------------------------------------------------------------
# Single writer thread: every SQLite write goes through here so worker threads never touch the
# connection. fatal_error surfaces a write failure to callers instead of dying silently mid-run.
# ---------------------------------------------------------------------------
class DBWriter(threading.Thread):
    def __init__(self, conn, batch_size=WRITE_BATCH_SIZE, max_retries=5, retry_backoff_s=2.0):
        super().__init__(daemon=True)
        self.conn = conn
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self.q = queue.Queue()
        self._stop_event = threading.Event()  # not self._stop — collides with Thread's own _stop()
        self.written = 0
        self.fatal_error = None  # set once _flush exhausts its retries

    def put(self, item):
        self.q.put(item)

    def stop_when_drained(self):
        self._stop_event.set()

    def run(self):
        pending = []
        last_commit = time.time()
        while self.fatal_error is None:
            try:
                item = self.q.get(timeout=1.0)
                pending.append(item)
            except queue.Empty:
                if self._stop_event.is_set() and self.q.empty():
                    break
                item = None

            if pending and (len(pending) >= self.batch_size or time.time() - last_commit > 5):
                if not self._flush(pending):
                    return  # fatal_error is set — stop, don't touch the connection again
                self.written += len(pending)
                pending = []
                last_commit = time.time()

        if pending:
            self._flush(pending)
            self.written += len(pending)

    def _flush(self, items):
        """Returns True on success; on exhausted retries sets self.fatal_error and returns False instead of raising."""
        nodes = [i[1] for i in items if i[0] == "node"]
        texts = [i[1] for i in items if i[0] == "text"]
        edges = [i[1] for i in items if i[0] == "edges"]
        progresses = [i[1] for i in items if i[0] == "progress"]
        flat_edges = [e for edge_list in edges for e in edge_list]

        for attempt in range(1, self.max_retries + 1):
            try:
                if nodes:
                    self.conn.executemany(
                        "INSERT OR IGNORE INTO nodes (path, title, node_type, classification_source, "
                        "lat, lon, date_start, date_end, date_source) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        nodes,
                    )
                if texts:
                    self.conn.executemany(
                        "UPDATE nodes SET text_compressed = ?, text_length = ? WHERE path = ?",
                        texts,
                    )
                if flat_edges:
                    self.conn.executemany(
                        "INSERT OR IGNORE INTO edges (source_path, target_path, edge_source) "
                        "VALUES (?, ?, ?)",
                        flat_edges,
                    )
                if progresses:
                    last_entry_id, phase = progresses[-1]
                    set_progress(self.conn, last_entry_id, phase)

                self.conn.commit()
                return True
            except sqlite3.Error as e:
                print(f"[DBWriter] write failed (attempt {attempt}/{self.max_retries}): {e}")
                try:
                    self.conn.rollback()
                except sqlite3.Error:
                    pass
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff_s * attempt)
                else:
                    print(f"[DBWriter] giving up after {self.max_retries} attempts — "
                          f"{len(items)} queued write(s) lost, stopping this writer")
                    self.fatal_error = e
                    return False


# ---------------------------------------------------------------------------
# Phase 1 — classify every entry; no text/link extraction yet.
# ---------------------------------------------------------------------------
def run_classify_phase(zim, conn, writer):
    total_entries = zim.all_entry_count
    last_entry_id, phase = get_progress(conn)
    if phase != "classify":
        print(f"[classify] already past this phase (progress phase={phase}) — skipping")
        return

    start_id = last_entry_id + 1
    end_id = total_entries if PHASE1_ENTRY_LIMIT is None else min(total_entries, start_id + PHASE1_ENTRY_LIMIT)
    print(f"[classify] scanning entries {start_id:,}..{end_id-1:,} of {total_entries:,} total "
          f"({MAX_WORKERS} workers)")

    found = 0
    processed = 0
    t0 = time.time()

    # One Archive handle per worker thread — libzim.Archive isn't documented as safe to share.
    thread_local = threading.local()

    def worker(entry_id):
        if not hasattr(thread_local, "zim"):
            thread_local.zim = open_archive()
        z = thread_local.zim
        try:
            entry = z._get_entry_by_id(entry_id)
        except Exception:
            return entry_id, None
        if entry.is_redirect:
            return entry_id, None
        path = entry.path
        if any(path.startswith(p) for p in BAD_PREFIXES):
            return entry_id, None
        try:
            item = entry.get_item()
            if not item.mimetype.startswith("text/html"):
                return entry_id, None
            html = bytes(item.content).decode("utf-8", errors="ignore")
        except Exception:
            return entry_id, None
        node_type, source = classify_node(html, entry.title)
        if node_type is None:
            return entry_id, None
        lat, lon = extract_coords(html)
        date_start, date_source = extract_founding_year(html)
        return entry_id, (path, entry.title, node_type, source, lat, lon, date_start, date_start, date_source)

    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        futures = {executor.submit(worker, i): i for i in range(start_id, end_id)}
        highest_done = start_id - 1
        for future in as_completed(futures):
            if writer.fatal_error is not None:
                # Writer gave up — cancel remaining futures rather than grinding through pointlessly.
                executor.shutdown(wait=False, cancel_futures=True)
                raise RuntimeError(
                    f"[classify] stopping: DBWriter failed permanently ({writer.fatal_error})"
                )

            entry_id, result = future.result()
            processed += 1
            if result is not None:
                writer.put(("node", result))
                found += 1
            highest_done = max(highest_done, entry_id)

            if processed % 5000 == 0:
                writer.put(("progress", (highest_done, "classify")))
                elapsed = time.time() - t0
                rate = processed / elapsed
                remaining = (end_id - start_id - processed) / rate if rate else float("inf")
                print(f"[classify] {processed:,}/{end_id-start_id:,} scanned, {found:,} classified "
                      f"({rate:.0f}/s, ~{remaining/60:.0f} min left)")
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    writer.put(("progress", (end_id - 1, "extract" if end_id >= total_entries else "classify")))
    print(f"[classify] done: {found:,} nodes found out of {processed:,} entries scanned "
          f"in {(time.time()-t0)/60:.1f} min")


# ---------------------------------------------------------------------------
# Phase 2 — pull text + filtered edges for every classified node.
# ---------------------------------------------------------------------------
def run_extract_phase(zim, conn, writer):
    last_entry_id, phase = get_progress(conn)
    if phase != "extract":
        print(f"[extract] not ready yet (progress phase={phase}) — finish classify first")
        return

    node_types = {row[0]: row[1] for row in conn.execute("SELECT path, node_type FROM nodes")}
    pending = [row[0] for row in conn.execute(
        "SELECT path FROM nodes WHERE text_compressed IS NULL"
    )]
    print(f"[extract] {len(node_types):,} total classified nodes, {len(pending):,} still need text/edges")

    thread_local = threading.local()

    def worker(path):
        if not hasattr(thread_local, "zim"):
            thread_local.zim = open_archive()
        z = thread_local.zim
        try:
            entry = z.get_entry_by_path(path)
            item = entry.get_item()
            html = bytes(item.content).decode("utf-8", errors="ignore")
        except Exception:
            return path, None, None

        text = extract_page_text(html)
        compressed = zlib.compress(text.encode("utf-8"))
        links = extract_links_proper(html)
        # Never a city-to-city link edge — e.g. "Genoa, Colorado" linking to "Genoa" the Italian city is a name-origin mention, not a real subnode relationship.
        this_type = node_types.get(path)
        target_edges = [
            (path, link, "link") for link in links
            if link in node_types and link != path
            and not (this_type == "city" and node_types.get(link) == "city")
        ]

        return path, (compressed, len(text)), target_edges

    processed = 0
    t0 = time.time()
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        futures = {executor.submit(worker, p): p for p in pending}
        for future in as_completed(futures):
            if writer.fatal_error is not None:
                executor.shutdown(wait=False, cancel_futures=True)
                raise RuntimeError(
                    f"[extract] stopping: DBWriter failed permanently ({writer.fatal_error})"
                )

            path, text_result, edges = future.result()
            processed += 1
            if text_result is not None:
                compressed, length = text_result
                writer.put(("text", (compressed, length, path)))
            if edges:
                writer.put(("edges", edges))

            if processed % 2000 == 0:
                elapsed = time.time() - t0
                rate = processed / elapsed
                remaining = (len(pending) - processed) / rate if rate else float("inf")
                print(f"[extract] {processed:,}/{len(pending):,} nodes filled "
                      f"({rate:.0f}/s, ~{remaining/60:.0f} min left)")
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    writer.put(("progress", (last_entry_id, "link")))
    print(f"[extract] done: filled text/edges for {processed:,} nodes in {(time.time()-t0)/60:.1f} min")


# ---------------------------------------------------------------------------
# Phase 3 — name cross-reference: connect non-city nodes to cities mentioning them, or vice versa.
# ---------------------------------------------------------------------------
def run_link_phase(conn, writer):
    last_entry_id, phase = get_progress(conn)
    if phase != "link":
        print(f"[link] not ready yet (progress phase={phase}) — finish extract first")
        return

    city_pairs = conn.execute(
        "SELECT path, title FROM nodes WHERE node_type = 'city' AND title IS NOT NULL"
    ).fetchall()
    noncity_pairs = conn.execute(
        "SELECT path, title FROM nodes WHERE node_type != 'city' AND title IS NOT NULL"
    ).fetchall()
    print(f"[link] {len(city_pairs):,} city nodes, {len(noncity_pairs):,} non-city nodes")

    print("[link] building automatons...")
    t0 = time.time()
    city_automaton = build_automaton(city_pairs)
    noncity_automaton = build_automaton(noncity_pairs)
    print(f"[link] automatons built in {time.time()-t0:.0f}s")

    def scan(label, rows, automaton):
        processed = 0
        t0 = time.time()
        thread_local = threading.local()

        def worker(path):
            if not hasattr(thread_local, "read_conn"):
                # Own connection per worker — sharing the writer's would contend on every read.
                thread_local.read_conn = sqlite3.connect(DB_PATH)
                _configure_connection(thread_local.read_conn)
            row = thread_local.read_conn.execute(
                "SELECT text_compressed FROM nodes WHERE path = ?", (path,)
            ).fetchone()
            if not row or row[0] is None:
                return path, []
            text = zlib.decompress(row[0]).decode("utf-8")
            return path, list(find_name_matches(automaton, text))

        executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        try:
            futures = {executor.submit(worker, path): path for path, _ in rows}
            for future in as_completed(futures):
                if writer.fatal_error is not None:
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise RuntimeError(
                        f"[link:{label}] stopping: DBWriter failed permanently ({writer.fatal_error})"
                    )

                path, matches = future.result()
                processed += 1
                if matches:
                    writer.put(("edges", [(path, m, "name_match") for m in matches]))

                if processed % 5000 == 0:
                    elapsed = time.time() - t0
                    rate = processed / elapsed
                    remaining = (len(rows) - processed) / rate if rate else float("inf")
                    print(f"[link:{label}] {processed:,}/{len(rows):,} scanned "
                          f"({rate:.0f}/s, ~{remaining/60:.0f} min left)")
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        print(f"[link:{label}] done: {processed:,} nodes scanned in {(time.time()-t0)/60:.1f} min")

    scan("city-text -> entities", city_pairs, noncity_automaton)
    scan("entity-text -> cities", noncity_pairs, city_automaton)

    writer.put(("progress", (last_entry_id, "done")))
    print("[link] done")


def print_summary(conn):
    print("\n--- graph summary: by node_type ---")
    for row in conn.execute("SELECT node_type, COUNT(*) FROM nodes GROUP BY node_type ORDER BY 2 DESC"):
        print(f"  {row[0] or '(unclassified)':10s} {row[1]:,}")
    total_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    print(f"  {'total':10s} {total_nodes:,}")

    print("--- by classification_source (confidence tier) ---")
    for row in conn.execute(
        "SELECT classification_source, COUNT(*) FROM nodes GROUP BY classification_source ORDER BY 2 DESC"
    ):
        print(f"  {row[0] or '(unknown)':10s} {row[1]:,}")

    print("--- edges by edge_source ---")
    for row in conn.execute("SELECT edge_source, COUNT(*) FROM edges GROUP BY edge_source ORDER BY 2 DESC"):
        print(f"  {row[0] or '(unknown)':10s} {row[1]:,}")

    # Role: "subordinate" (>=1 edge to a city) vs "extra"/orphan (no city connection).
    subordinate = conn.execute(
        """
        SELECT COUNT(*) FROM nodes n
        WHERE n.node_type != 'city' AND EXISTS (
            SELECT 1 FROM edges e
            JOIN nodes c ON c.path = CASE WHEN e.source_path = n.path THEN e.target_path ELSE e.source_path END
            WHERE (e.source_path = n.path OR e.target_path = n.path) AND c.node_type = 'city'
        )
        """
    ).fetchone()[0]
    total_noncity = conn.execute("SELECT COUNT(*) FROM nodes WHERE node_type != 'city'").fetchone()[0]
    print("--- non-city node roles ---")
    print(f"  subordinate (linked to >=1 city) {subordinate:,}")
    print(f"  extra/orphan (no city found)     {total_noncity - subordinate:,}")

    print(f"-> {DB_PATH}")


def _check_writer(writer):
    """Raises if the writer failed during its final drain, which the mid-scan checks miss."""
    if writer.fatal_error is not None:
        raise RuntimeError(f"DBWriter failed permanently: {writer.fatal_error}")


# ---------------------------------------------------------------------------
# split_title_disambiguation is still used by the disambiguator-linking pass below. The reclassify
# pass that used to live here (fixing 'city' nodes that are actually known countries/regions, e.g.
# "Canada") was removed along with MAIN_DATASET_PATH — it depended on an external ~150MB reference
# dataset (every history[]/region_history[].entity name from a separate project) that doesn't ship
# with this repo and isn't part of what this pipeline is self-contained enough to run without.
# ---------------------------------------------------------------------------
def split_title_disambiguation(title):
    """"Name, Disambiguator" -> (core_name, disambiguator_or_None); only the first comma matters."""
    if not title or "," not in title:
        return title, None
    core, _, disambiguator = title.partition(",")
    return core.strip(), disambiguator.strip()


# ---------------------------------------------------------------------------
# Disambiguator-linking pass — connects a city to its graph's own node for its Wikipedia disambiguator suffix (e.g. "Burswood, Western Australia" -> the graph's Western Australia node).
# ---------------------------------------------------------------------------
def link_disambiguators(conn):
    """Adds a disambiguator_match edge from each city to the best-matching non-city title (if any scores above DISAMBIGUATOR_MATCH_MIN_SCORE). Returns the (city_path, target_path) pairs added."""
    noncity_rows = conn.execute(
        "SELECT path, title FROM nodes WHERE node_type != 'city' AND title IS NOT NULL"
    ).fetchall()
    if not noncity_rows:
        print("[link_disambiguators] no non-city nodes to match against yet — skipping")
        return []

    title_to_paths = {}
    for path, title in noncity_rows:
        title_to_paths.setdefault(title, []).append(path)

    DISAMBIGUATOR_MATCHER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    matcher = NameMatcher(
        [title for _, title in noncity_rows], cache_path=DISAMBIGUATOR_MATCHER_CACHE_PATH
    )

    city_rows = conn.execute(
        "SELECT path, title FROM nodes WHERE node_type = 'city' AND title IS NOT NULL"
    ).fetchall()
    print(f"Checking {len(city_rows):,} city titles' disambiguators against "
          f"{len(noncity_rows):,} in-graph non-city node titles "
          f"(min_score={DISAMBIGUATOR_MATCH_MIN_SCORE})...")

    new_edges = []
    for city_path, city_title in city_rows:
        _core, disambiguator = split_title_disambiguation(city_title)
        if not disambiguator:
            continue

        match = matcher.best_match(
            disambiguator, min_score=DISAMBIGUATOR_MATCH_MIN_SCORE, noise_terms=STATE_NOISE_TERMS
        )
        if not match:
            continue

        for target_path in title_to_paths.get(match["name"], []):
            if target_path != city_path:
                new_edges.append((city_path, target_path, "disambiguator_match"))

    if new_edges:
        conn.executemany(
            "INSERT OR IGNORE INTO edges (source_path, target_path, edge_source) VALUES (?, ?, ?)",
            new_edges,
        )
        conn.commit()

    matcher.save_cache()
    print(f"Added {len(new_edges)} disambiguator-match edge(s)")
    return new_edges


def main():
    conn = open_db()
    writer = DBWriter(conn)
    writer.start()

    zim = open_archive()
    source_label = ZIM_PATH if SOURCE_FORMAT != "sample" else str(SAMPLE_DIR)
    print(f"Opened {source_label} ({zim.article_count:,} articles) [SOURCE_FORMAT={SOURCE_FORMAT!r}]")

    try:
        run_classify_phase(zim, conn, writer)
        writer.stop_when_drained()
        writer.join()
        _check_writer(writer)

        # A fresh writer for each phase — the previous one already stopped and drained.
        writer = DBWriter(conn)
        writer.start()
        run_extract_phase(zim, conn, writer)
        writer.stop_when_drained()
        writer.join()
        _check_writer(writer)

        writer = DBWriter(conn)
        writer.start()
        run_link_phase(conn, writer)
        writer.stop_when_drained()
        writer.join()
        _check_writer(writer)

        if RUN_DISAMBIGUATOR_LINK_PASS:
            link_disambiguators(conn)
    finally:
        writer.stop_when_drained()
        writer.join()
        print_summary(conn)
        conn.close()


if __name__ == "__main__":
    main()
