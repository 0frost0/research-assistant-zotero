"""BM25 + reciprocal rank fusion over canonical, active evidence IDs.

No new vector collection, model, or synthetic evidence. Lexical matches are
candidate proposals, never a claim that the question is answerable.
"""
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
import math
import re
import unicodedata


VERSION = "bm25-rrf-v1"
TOKENIZER_VERSION = "latin-plural-cjk-bigram-v1"


def tokens(text):
    text = unicodedata.normalize("NFKC", text).lower()
    result = []
    for term in re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*|[\u4e00-\u9fff]+", text):
        if re.fullmatch(r"[\u4e00-\u9fff]+", term):
            result.extend(term[i:i + 2] for i in range(len(term) - 1))
            if len(term) == 1:
                result.append(term)
        else:
            # Preserve identifiers AND their constituent terms (MIMIC-III).
            for word in dict.fromkeys([term, *re.split(r"[-_.]", term)]):
                if len(word) >= 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
                    word = word[:-1]
                result.append(word)
    return result


def hit_dict(value):
    return asdict(value) if is_dataclass(value) else dict(value)


class BM25Index:
    def __init__(self, records, *, k1=1.2, b=0.75):
        self.records = [hit_dict(r) for r in records]
        self.records = [r for r in self.records if r.get("content_type", "text") not in
                        {"header", "footer", "page_number", "image", "chart"}
                        and r.get("modality", "text") == "text"]
        self.k1, self.b = k1, b
        self.lengths, self.postings = [], defaultdict(list)
        for index, record in enumerate(self.records):
            counts = Counter(tokens(record["text"]))
            self.lengths.append(sum(counts.values()))
            for term, count in counts.items():
                self.postings[term].append((index, count))
        self.average_length = sum(self.lengths) / max(len(self.records), 1) or 1

    def search(self, query, *, k=40, sources=None, file_types=None):
        if sources == [] or file_types == []:
            return []
        allowed = set(sources) if sources is not None else None
        extensions = {v.lower() if v.startswith(".") else "." + v.lower() for v in file_types} if file_types else None
        scores = defaultdict(float)
        for term in set(tokens(query)):
            posting = self.postings.get(term, [])
            idf = math.log(1 + (len(self.records) - len(posting) + .5) / (len(posting) + .5))
            for index, frequency in posting:
                record = self.records[index]
                if allowed is not None and record["source"] not in allowed:
                    continue
                if extensions is not None and record["file_type"].lower() not in extensions:
                    continue
                norm = self.k1 * (1 - self.b + self.b * self.lengths[index] / self.average_length)
                scores[index] += idf * frequency * (self.k1 + 1) / (frequency + norm)
        ranking = sorted(scores, key=lambda i: (-scores[i], self.records[i]["point_id"]))[:k]
        return [{**self.records[i], "score": scores[i], "bm25_score": scores[i]} for i in ranking if scores[i] > 0]


def reciprocal_rank_fusion(dense, lexical, *, limit=80, rank_constant=60):
    """Fuse ranks, not incomparable BM25/cosine scores. Keep native scores."""
    merged = {}
    for channel, values in (("dense", dense), ("lexical", lexical)):
        seen = set()
        for rank, value in enumerate(values, 1):
            hit = hit_dict(value)
            ident = hit["point_id"]
            if ident in seen:
                continue
            seen.add(ident)
            if ident not in merged:
                merged[ident] = {**hit, "ranking": {"policy": VERSION, "rrf_score": 0.0}}
            details = merged[ident]["ranking"]
            details[channel + "_rank"] = rank
            details["dense_score" if channel == "dense" else "bm25_score"] = hit["score"]
            details["rrf_score"] += 1 / (rank_constant + rank)
    result = sorted(merged.values(), key=lambda h: (-h["ranking"]["rrf_score"], h["point_id"]))[:limit]
    for hit in result:
        # Only a display/ranking score. Never threshold as cosine/probability.
        hit["score"] = hit["ranking"]["rrf_score"] / (2 / (rank_constant + 1))
        hit["ranking"]["score_kind"] = "normalized_rrf_not_probability"
    return result
