import pickle
import re
import unicodedata
from pathlib import Path

import numpy as np
from rapidfuzz import fuzz, process

try:
    import ollama
except ImportError:
    ollama = None

EMBED_MODEL = "all-minilm"  # small local model: `ollama pull all-minilm` (also used by DEV/SEARCHER)
EMBED_DIM = 384

STATE_NOISE_TERMS = [
    "early", "later", "late", "march", "grand", "duchy", "republic", "kingdom", "federation",
    "federative", "state", "shogunate", "khanate", "caliphate", "triple", "alliance", "clan",
    "lordship", "the", "order", "margraviate", "electorate", "confederation", "league",
    "commonwealth", "principality", "government", "of", "islamic", "federal", "tribes",
    "tsardom", "sultanate", "emirate", "empire", "crown", "democratic", "people's", "ancient",
    "new", "civilization", "imperial", "dynastic", "dynasty", "socialist", "peoples", "union",
    "republics", "united", "states", "oriental", "trust", "territory", "great", "soviet", "north",
    "continental", "people", "petty", "northern", "period", "southern", "indegenous",
    "northeastern", "northwestern", "southeastern", "southwestern", "culture", "city-state",
    "eastern", "western", "central", "east", "west",
]


def normalize_text(text):
    result = unicodedata.normalize("NFKD", text)
    return result.encode("ascii", "ignore").decode("ascii")


def strip_noise(text, noise_terms):
    if not noise_terms:
        return text
    pattern = "|".join(re.escape(t) for t in noise_terms)
    cleaned = re.sub(rf"\b(?:{pattern})\b", " ", text)
    return " ".join(cleaned.split())


def _embed(texts, batch_size=256):
    if ollama is None:
        raise RuntimeError("ollama package not installed -- `pip install ollama` (server + `ollama pull all-minilm` also required)")
    out = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        try:
            out.extend(ollama.embed(model=EMBED_MODEL, input=batch)["embeddings"])
        except Exception:
            for t in batch:
                out.extend(ollama.embed(model=EMBED_MODEL, input=[t])["embeddings"])
    return np.array(out, dtype="float32")


class NameMatcher:
    """
    A single, domain-agnostic corpus of names -- mix states, regions, cities, anything, in one
    instance; nothing about storage or matching assumes a domain.

    names: initial corpus (list of str). Can be empty -- e.g. the "adding new data" use case starts
        empty and grows via add_names/classify_batch as new records are processed.
    fuzzy_weight/embed_weight: how the two stages combine into `score` (0..1 each candidate).
    cache_path: optional path to persist embeddings across runs (pickle of cleaned-text -> vector).
        Call save_cache() when done to write it; skips re-embedding known names on the next run.
    """

    def __init__(self, names=None, fuzzy_weight=0.5, embed_weight=0.5, cache_path=None):
        self.fuzzy_weight = fuzzy_weight
        self.embed_weight = embed_weight
        self.cache_path = Path(cache_path) if cache_path else None
        self._embedding_cache = {}  # cleaned_text -> unit vector
        self._embeddings_available = True  # flips off (with a one-time warning) if Ollama is unreachable

        if self.cache_path and self.cache_path.exists():
            with open(self.cache_path, "rb") as f:
                self._embedding_cache = pickle.load(f)

        self.names = []     # original strings, in corpus order -- what search() returns
        self.cleaned = []   # normalized + lowercased, parallel to self.names -- what matching runs on
        self.embeddings = np.zeros((0, EMBED_DIM), dtype="float32")

        if names:
            self.add_names(names)

    def __len__(self):
        return len(self.names)

    def save_cache(self):
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cache_path, "wb") as f:
            pickle.dump(self._embedding_cache, f)

    def _clean(self, name):
        return normalize_text(name).strip().lower()

    def _embed_cleaned(self, cleaned_texts):
        to_embed = [t for t in dict.fromkeys(cleaned_texts) if t not in self._embedding_cache]
        if to_embed and self._embeddings_available:
            try:
                vectors = _embed(to_embed)
                norms = np.clip(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-9, None)
                vectors = vectors / norms
                for text, vec in zip(to_embed, vectors):
                    self._embedding_cache[text] = vec
            except Exception as e:
                self._embeddings_available = False
                print(f"[names_search] embeddings unavailable ({e}); falling back to fuzzy-only matching")
        for text in to_embed:
            if text not in self._embedding_cache:
                self._embedding_cache[text] = np.zeros(EMBED_DIM, dtype="float32")
        return np.array([self._embedding_cache[t] for t in cleaned_texts], dtype="float32")

    def _score(self, fscore, sim):
        if not self._embeddings_available:
            return fscore / 100.0
        return self.fuzzy_weight * (fscore / 100.0) + self.embed_weight * max(sim, 0.0)

    def add_names(self, names):
        """Adds names to the corpus, skipping ones already present (exact match on original string).
        Names of any kind can be mixed into the same call/corpus -- nothing here is domain-specific."""
        seen = set(self.names)
        new_names = [n.strip() for n in names if n and n.strip() and n.strip() not in seen and not seen.add(n.strip())]
        if not new_names:
            return

        new_cleaned = [self._clean(n) for n in new_names]
        new_embeddings = self._embed_cleaned(new_cleaned)

        self.names.extend(new_names)
        self.cleaned.extend(new_cleaned)
        self.embeddings = np.vstack([self.embeddings, new_embeddings]) if len(self.embeddings) else new_embeddings

    def search(self, name, top_k=10, min_score=0.55, shortlist_size=100, noise_terms=None):
        if not self.names:
            return []

        query_plain = self._clean(name)
        matches = process.extract(query_plain, self.cleaned, scorer=fuzz.WRatio, limit=shortlist_size, score_cutoff=0)

        query_vec = self._embed_cleaned([query_plain])[0]
        query_stripped = strip_noise(query_plain, noise_terms) if noise_terms else None
        query_vec_stripped = self._embed_cleaned([query_stripped])[0] if query_stripped else None

        results = []
        seen_names = set()
        for _, fscore, idx in matches:
            display_name = self.names[idx]
            if display_name in seen_names:
                continue
            seen_names.add(display_name)

            sim = float(self.embeddings[idx] @ query_vec) if self._embeddings_available else 0.0
            best_fscore, best_sim = fscore, sim

            if query_vec_stripped is not None:
                cand_stripped = strip_noise(self.cleaned[idx], noise_terms)
                fscore_s = fuzz.WRatio(query_stripped, cand_stripped)
                cand_vec_s = self._embed_cleaned([cand_stripped])[0]
                sim_s = float(cand_vec_s @ query_vec_stripped) if self._embeddings_available else 0.0
                if self._score(fscore_s, sim_s) > self._score(best_fscore, best_sim):
                    best_fscore, best_sim = fscore_s, sim_s

            combined = self._score(best_fscore, best_sim)
            results.append({"name": display_name, "score": round(combined, 4), "fuzzy_score": best_fscore, "embed_sim": round(best_sim, 4)})

        results.sort(key=lambda r: r["score"], reverse=True)
        return [r for r in results if r["score"] >= min_score][:top_k]

    def best_match(self, name, min_score=0.55, noise_terms=None):
        results = self.search(name, top_k=1, min_score=min_score, noise_terms=noise_terms)
        return results[0] if results else None

    def is_unique(self, name, min_score=0.55, noise_terms=None):
        return self.best_match(name, min_score=min_score, noise_terms=noise_terms) is None

    def classify_batch(self, names, min_score=0.55, add_unique=True, noise_terms=None, top_k=10):
        out = []
        for name in names:
            matches = self.search(name, top_k=top_k, min_score=min_score, noise_terms=noise_terms)
            out.append({"name": name, "unique": len(matches) == 0, "found_names": [m["name"] for m in matches]})
            if add_unique and not matches:
                self.add_names([name])
        return out


if __name__ == "__main__":
    # Tiny demo: fuzzy matching works without Ollama; embeddings are used when it is available.
    matcher = NameMatcher(["France", "Germany", "Tokyo", "Minas Gerais", "Kingdom of Jin"])
    print(f"corpus: {len(matcher)} names")
    for row in matcher.classify_batch(["Kingdom of France", "Minas Gerias", "Wakanda"],
                                      min_score=0.75, noise_terms=STATE_NOISE_TERMS):
        print(row)
