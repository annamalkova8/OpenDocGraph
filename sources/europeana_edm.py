import glob
import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from .base import ExtractedEdge, ExtractedNode, SourceAdapter

ENTITY_API_URL = "https://api.europeana.eu/entity/agent/base/{id}.json?wskey=api2demo"
_AGENT_URI_RE = re.compile(r"^https?://data\.europeana\.eu/agent/(\d+)$")
_DEFAULT_ENTITY_CACHE_PATH = Path(__file__).resolve().parent.parent / "output" / "europeana_entity_cache.json"

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "edm": "http://www.europeana.eu/schemas/edm/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "wgs84": "http://www.w3.org/2003/01/geo/wgs84_pos#",
    "ore": "http://www.openarchives.org/ore/terms/",
    "foaf": "http://xmlns.com/foaf/0.1/",
}
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_RDF_ABOUT = f"{{{NS['rdf']}}}about"
_RDF_RESOURCE = f"{{{NS['rdf']}}}resource"

_YEAR_TOKEN_RE = re.compile(r"(\d{3,4})\s*(BCE|BC)?", re.IGNORECASE)
_YEAR_RANGE_RE = re.compile(
    r"(?:c\.?\s*)?(\d{3,4})\s*(BCE|BC)?\s*[-–—]\s*(?:c\.?\s*)?(\d{3,4})\s*(BCE|BC)?",
    re.IGNORECASE,
)


def _year_from_text(text):
    matches = list(_YEAR_TOKEN_RE.finditer(text or ""))
    if not matches:
        return None
    m = matches[-1]
    year = int(m.group(1))
    return -year if m.group(2) else year


def _year_range_from_text(text):
    m = _YEAR_RANGE_RE.search(text or "")
    if not m:
        return None, None
    start = int(m.group(1))
    if m.group(2):
        start = -start
    end = int(m.group(3))
    if m.group(4):
        end = -end
    return start, end


def _agent_id_from_uri(uri):
    m = _AGENT_URI_RE.match(uri or "")
    return m.group(1) if m else None


def _year_from_entity_date(text):
    if not text:
        return None
    negative = text.startswith("-")
    core = text[1:] if negative else text
    year_part = core.split("-")[0]
    try:
        year = int(year_part)
    except ValueError:
        return None
    return -year if negative else year


def _pref_label(el, prefer_lang="en"):
    labels = el.findall("skos:prefLabel", NS)
    fallback = None
    for label in labels:
        if fallback is None:
            fallback = label.text
        if label.get(_XML_LANG) == prefer_lang:
            return label.text
    return fallback


def _lang_texts(elements, prefer_lang="en"):
    preferred, all_texts = None, []
    for el in elements:
        if el.text:
            all_texts.append(el.text)
            if preferred is None or el.get(_XML_LANG) == prefer_lang:
                preferred = el.text
    return preferred, all_texts


def _resolve_date_range(el, concepts_by_uri, timespans_by_uri):
    if el is None:
        return None, None
    if el.text:
        start, end = _year_range_from_text(el.text)
        if start is not None:
            return start, end
        year = _year_from_text(el.text)
        return (year, year) if year is not None else (None, None)
    uri = el.get(_RDF_RESOURCE)
    if not uri:
        return None, None
    ts = timespans_by_uri.get(uri)
    if ts and (ts["begin"] or ts["end"]):
        start = _year_from_text(ts["begin"]) if ts["begin"] else None
        end = _year_from_text(ts["end"]) if ts["end"] else start
        return start, (end if end is not None else start)
    label = concepts_by_uri.get(uri)
    if label:
        year = _year_from_text(label)
        return (year, year) if year is not None else (None, None)
    return None, None


class EuropeanaEdmSource(SourceAdapter):
    def __init__(
        self, dataset_dirs, resolve_creators=False, entity_cache_path=None,
        extract_dates=True, extract_places=True,
    ):
        self.dataset_dirs = dataset_dirs
        self.resolve_creators = resolve_creators
        self.extract_dates = extract_dates
        self.extract_places = extract_places
        self.entity_cache_path = Path(entity_cache_path or _DEFAULT_ENTITY_CACHE_PATH)
        self._entity_cache = {}
        if self.entity_cache_path.exists():
            self._entity_cache = json.loads(self.entity_cache_path.read_text(encoding="utf-8"))

    def iter_nodes(self):
        self._place_nodes_emitted = set()
        self._place_nodes_pending = []
        self._person_nodes_emitted = set()
        self._person_nodes_pending = []
        self._pending_edges = []
        for dataset_dir in self.dataset_dirs:
            for path in sorted(glob.glob(os.path.join(dataset_dir, "*.xml"))):
                node = self._parse_record(path)
                if node is not None:
                    yield node
            for place_node in self._flush_pending(self._place_nodes_pending):
                yield place_node
            for person_node in self._flush_pending(self._person_nodes_pending):
                yield person_node
        self._save_entity_cache()

    def iter_edges(self, known_paths):
        for edge in self._pending_edges:
            if edge.source_path in known_paths and edge.target_path in known_paths:
                yield edge

    @staticmethod
    def _flush_pending(pending_list):
        nodes, pending_list[:] = list(pending_list), []
        return nodes

    def _save_entity_cache(self):
        if not self._entity_cache:
            return
        self.entity_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.entity_cache_path.write_text(
            json.dumps(self._entity_cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _resolve_agent(self, uri):
        if uri in self._entity_cache:
            return self._entity_cache[uri]
        if not self.resolve_creators:
            return None
        agent_id = _agent_id_from_uri(uri)
        if agent_id is None:
            self._entity_cache[uri] = None
            return None
        try:
            request = urllib.request.Request(
                ENTITY_API_URL.format(id=agent_id), headers={"Accept": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            print(f"[europeana_edm] entity resolution failed for {uri}: {e}")
            self._entity_cache[uri] = None
            return None

        pref_label = data.get("prefLabel") or {}
        resolved = {
            "name": pref_label.get("en") or next(iter(pref_label.values()), None),
            "birth_year": _year_from_entity_date(data.get("dateOfBirth")),
            "death_year": _year_from_entity_date(data.get("dateOfDeath")),
            "profession": data.get("professionOrOccupation"),
        }
        self._entity_cache[uri] = resolved
        return resolved

    def _parse_record(self, path):
        root = ET.parse(path).getroot()

        cho = root.find("edm:ProvidedCHO", NS)
        if cho is None:
            return None
        record_path = cho.get(_RDF_ABOUT, "").split("/item/")[-1]
        if not record_path:
            return None

        places_by_uri = {}
        for p in root.findall("edm:Place", NS):
            uri = p.get(_RDF_ABOUT)
            lat_el, lon_el = p.find("wgs84:lat", NS), p.find("wgs84:long", NS)
            places_by_uri[uri] = {
                "label": _pref_label(p),
                "lat": float(lat_el.text) if lat_el is not None else None,
                "lon": float(lon_el.text) if lon_el is not None else None,
            }
        orgs_by_uri = {
            o.get(_RDF_ABOUT): _pref_label(o) for o in root.findall("foaf:Organization", NS)
        }
        timespans_by_uri = {}
        for ts in root.findall("edm:TimeSpan", NS):
            begin, end = ts.find("edm:begin", NS), ts.find("edm:end", NS)
            timespans_by_uri[ts.get(_RDF_ABOUT)] = {
                "begin": begin.text if begin is not None else None,
                "end": end.text if end is not None else None,
            }
        concepts_by_uri = {
            c.get(_RDF_ABOUT): _pref_label(c) for c in root.findall("skos:Concept", NS)
        }

        provider_proxy, europeana_proxy = None, None
        for proxy in root.findall("ore:Proxy", NS):
            about = proxy.get(_RDF_ABOUT, "")
            if "/proxy/europeana/" in about:
                europeana_proxy = proxy
            else:
                provider_proxy = proxy
        if provider_proxy is None:
            return None

        title, _ = _lang_texts(provider_proxy.findall("dc:title", NS))
        edm_type_el = provider_proxy.find("edm:type", NS)
        edm_type = edm_type_el.text if edm_type_el is not None else None

        creators = [c.text for c in provider_proxy.findall("dc:creator", NS) if c.text]
        creator_uris = list(dict.fromkeys(
            c.get(_RDF_RESOURCE)
            for proxy in (provider_proxy, europeana_proxy)
            if proxy is not None
            for c in proxy.findall("dc:creator", NS)
            if c.get(_RDF_RESOURCE) and _agent_id_from_uri(c.get(_RDF_RESOURCE))
        ))
        provider_agg_el = root.find("ore:Aggregation", NS)
        rights_el = provider_agg_el.find("edm:rights", NS) if provider_agg_el is not None else None
        rights = rights_el.get(_RDF_RESOURCE) if rights_el is not None else None
        data_provider_el = (
            provider_agg_el.find("edm:dataProvider", NS) if provider_agg_el is not None else None
        )
        provider_el = (
            provider_agg_el.find("edm:provider", NS) if provider_agg_el is not None else None
        )
        data_provider = orgs_by_uri.get(
            data_provider_el.get(_RDF_RESOURCE) if data_provider_el is not None else None
        )
        provider = orgs_by_uri.get(
            provider_el.get(_RDF_RESOURCE) if provider_el is not None else None
        )

        agg_el = root.find("edm:EuropeanaAggregation", NS)
        country_el = agg_el.find("edm:country", NS) if agg_el is not None else None
        language_el = agg_el.find("edm:language", NS) if agg_el is not None else None
        landing_el = agg_el.find("edm:landingPage", NS) if agg_el is not None else None
        dataset_name_el = agg_el.find("edm:datasetName", NS) if agg_el is not None else None

        fulltext_url = None
        for wr in root.findall("edm:WebResource", NS):
            type_el = wr.find("rdf:type", NS)
            if type_el is not None and type_el.get(_RDF_RESOURCE, "").endswith("FullTextResource"):
                fulltext_url = wr.get(_RDF_ABOUT)
                break

        date_start = date_end = date_source = None
        if self.extract_dates:
            year_el = europeana_proxy.find("edm:year", NS) if europeana_proxy is not None else None
            if year_el is not None and year_el.text:
                date_start = date_end = int(year_el.text)
                date_source = "edm_year"

            if date_start is None:
                for tag in ("dc:date", "dcterms:created", "dcterms:issued"):
                    for el in provider_proxy.findall(tag, NS):
                        s, e = _resolve_date_range(el, concepts_by_uri, timespans_by_uri)
                        if s is not None:
                            date_start, date_end, date_source = s, e, tag.replace(":", "_")
                            break
                    if date_start is not None:
                        break

            if date_start is None:
                temporal_el = provider_proxy.find("dcterms:temporal", NS)
                s, e = _resolve_date_range(temporal_el, concepts_by_uri, timespans_by_uri)
                if s is not None:
                    date_start, date_end, date_source = s, e, "dcterms_temporal_coverage"

        subject_place, spatial_places, spatial_text = None, [], []
        provenance_text = ""
        if self.extract_places:
            for cov in provider_proxy.findall("dc:coverage", NS):
                uri = cov.get(_RDF_RESOURCE)
                place = places_by_uri.get(uri) if uri else None
                if place and place["lat"] is not None:
                    subject_place = {"uri": uri, **place}
                    break

            provenance_el = provider_proxy.find("dcterms:provenance", NS)
            provenance_text = (provenance_el.text or "") if provenance_el is not None else ""
            for spat in provider_proxy.findall("dcterms:spatial", NS):
                uri = spat.get(_RDF_RESOURCE)
                if uri:
                    place = places_by_uri.get(uri)
                    if place:
                        role = "custody" if place["label"] and place["label"] in provenance_text else "spatial"
                        spatial_places.append({"uri": uri, "role": role, **place})
                elif spat.text:
                    spatial_text.append(spat.text)

        creators_resolved = []
        for creator_uri in creator_uris:
            resolved = self._resolve_agent(creator_uri)
            if resolved is not None:
                creators_resolved.append({"uri": creator_uri, **resolved})

        node = ExtractedNode(
            path=record_path,
            title=title,
            node_type="artifact",
            classification_source=f"edm_type:{edm_type}" if edm_type else "edm_type:unknown",
            date_start=date_start,
            date_end=date_end,
            date_source=date_source,
            extra={
                "edm_type": edm_type,
                "creators": creators,
                "creators_resolved": creators_resolved,
                "rights": rights,
                "data_provider": data_provider,
                "provider": provider,
                "dataset_name": dataset_name_el.text if dataset_name_el is not None else None,
                "country": country_el.text if country_el is not None else None,
                "language": language_el.text if language_el is not None else None,
                "landing_page": landing_el.get(_RDF_RESOURCE) if landing_el is not None else None,
                "fulltext_url": fulltext_url,
                "provenance_text": provenance_text or None,
                "spatial_places": spatial_places,
                "spatial_text": spatial_text,
                "subject_coverage_uri": subject_place["uri"] if subject_place else None,
            },
        )

        if subject_place:
            place_path = f"place/{subject_place['uri']}"
            node.extra["subject_place_path"] = place_path
            self._pending_edges.append(
                ExtractedEdge(record_path, place_path, "dc_coverage")
            )
            if place_path not in self._place_nodes_emitted:
                self._place_nodes_emitted.add(place_path)
                self._place_nodes_pending.append(
                    ExtractedNode(
                        path=place_path,
                        title=subject_place["label"],
                        node_type="place",
                        classification_source="edm_place_coords",
                        lat=subject_place["lat"],
                        lon=subject_place["lon"],
                        extra={"source_uri": subject_place["uri"]},
                    )
                )

        for resolved in creators_resolved:
            person_path = f"person/{resolved['uri']}"
            self._pending_edges.append(ExtractedEdge(record_path, person_path, "dc_creator"))
            if person_path not in self._person_nodes_emitted:
                self._person_nodes_emitted.add(person_path)
                self._person_nodes_pending.append(
                    ExtractedNode(
                        path=person_path,
                        title=resolved["name"],
                        node_type="person",
                        classification_source="europeana_entity_api",
                        date_start=resolved["birth_year"],
                        date_end=resolved["death_year"],
                        date_source="entity_api_birth_death" if resolved["birth_year"] else None,
                        extra={"source_uri": resolved["uri"], "profession": resolved["profession"]},
                    )
                )

        return node
