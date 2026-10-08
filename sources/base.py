from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ExtractedNode:
    path: str
    title: Optional[str]
    node_type: str
    classification_source: str
    lat: Optional[float] = None
    lon: Optional[float] = None
    date_start: Optional[int] = None  # astronomical year, BCE negative
    date_end: Optional[int] = None
    date_source: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedEdge:
    source_path: str
    target_path: str
    edge_source: str


class SourceAdapter:
    def iter_nodes(self):
        raise NotImplementedError

    def iter_edges(self, known_paths: set[str]):
        return iter(())
