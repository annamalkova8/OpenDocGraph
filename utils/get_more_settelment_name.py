import numpy as np
from sentence_transformers import SentenceTransformer

MODEL_NAME = "BAAI/bge-m3"

standart_terms = [
    "town",
    "city",
    "village",
    "settlement",
]


 

class SettlementTermSearch:
    def __init__(self, terms_list=standart_terms):
        self.model = SentenceTransformer(MODEL_NAME)
        self.terms_list = terms_list
        self.terms_embeddings = self.model.encode(
            terms_list,
            normalize_embeddings=True,
        )

    def search_for_best_term(self, names_list):
        """Ищет наиболее похожий термин из terms_list для заданного имени."""
        scores_list = []
        for name in names_list:
            query_embedding = self.model.encode(
                name.strip().lower(),
                normalize_embeddings=True,
            )
            scores = self.terms_embeddings @ query_embedding
            best_index = int(np.argmax(scores))
            best_score = float(scores[best_index])
            scores_list.append({
                "name": name,
                "best_term": self.terms_list[best_index],
                "best_score": best_score,
            })
        result = ""
        current_best_score = -10000
        for score_item in scores_list:
            if score_item["best_score"] > current_best_score:
                current_best_score = score_item["best_score"]
                result = score_item["name"]
        return result


if __name__ == "__main__":
    term_searcher = SettlementTermSearch()

    names = [
        "Gustav Adolf Grammar School",
        "Kalevi Keskstaadion",
        "Tallinn"
    ]
    names = [
            "New York",
            "New York City",
            "JFK International Airport",
            "Brooklyn"
        ]
    print(term_searcher.search_for_best_term(names))
