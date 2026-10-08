import glob
import json
import os

from .base import ExtractedEdge, ExtractedNode, SourceAdapter


class WikiHtmlSampleSource(SourceAdapter):
    def __init__(self, sample_dir, extract_coords=True, extract_dates=True):
        self.sample_dir = sample_dir
        self.should_extract_coords = extract_coords
        self.should_extract_dates = extract_dates

    def iter_nodes(self):
        from utils.geohistorical_extractor import classify_node, extract_coords, extract_founding_year
        from utils.extract_links import extract_links_proper

        manifest_path = os.path.join(self.sample_dir, "manifest.json")
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)

        self._pending_edges = []
        node_types = {}
        parsed = []

        for html_path in sorted(glob.glob(os.path.join(self.sample_dir, "*.html"))):
            fname = os.path.basename(html_path)
            meta = manifest.get(fname)
            if meta is None:
                continue
            path = meta["zim_path"]
            display_title = meta["title"]  # not the filename — classify_node needs real spacing

            with open(html_path, encoding="utf-8") as f:
                html = f.read()

            node_type, source = classify_node(html, display_title)
            if node_type is None:
                continue
            lat, lon = extract_coords(html) if self.should_extract_coords else (None, None)
            date_start, date_source = (
                extract_founding_year(html) if self.should_extract_dates else (None, None)
            )

            node_types[path] = node_type
            parsed.append((path, display_title, node_type, source, lat, lon, date_start, date_source, html))

        for path, display_title, node_type, source, lat, lon, date_start, date_source, html in parsed:
            for link in extract_links_proper(html):
                if (
                    link in node_types
                    and link != path
                    and not (node_type == "city" and node_types[link] == "city")
                ):
                    self._pending_edges.append(ExtractedEdge(path, link, "link"))

            yield ExtractedNode(
                path=path,
                title=display_title,
                node_type=node_type,
                classification_source=source,
                lat=lat,
                lon=lon,
                date_start=date_start,
                date_end=date_start,
                date_source=date_source,
            )

    def iter_edges(self, known_paths):
        for edge in self._pending_edges:
            if edge.source_path in known_paths and edge.target_path in known_paths:
                yield edge
