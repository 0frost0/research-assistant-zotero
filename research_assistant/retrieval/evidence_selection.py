"""Query-conditioned, budgeted greedy selection of complementary passages.

Borrow the set-selection objective, not SetR's trained selector. Lexical novelty
is a proxy for complementarity; it does not establish semantic sufficiency.
"""
import math
from collections import Counter

from research_assistant.retrieval.hybrid_retrieval import tokens
from research_assistant.retrieval.query_intent import definition_rank


VERSION = "coverage-mmr-v1"
STOPWORDS = set("a an the of is are was were to in on for and or with what which how does do as at by from "
                "this that it its be been has have had can could would should not".split())


def features(text):
    return set(tokens(text)) - STOPWORDS


def select_complementary(hits, question, *, limit=6, char_budget=12000):
    """Greedy relevance + marginal query coverage - maximum redundancy.

    Use the same length and count budgets as the legacy selector. Never invent
    spans, drop qualifiers, or enforce a quota per paper.
    """
    if not hits:
        return []
    if limit <= 0 or char_budget <= 0:
        return []
    # Preserve the old definition preference when no learned ranking is available.
    if not any("rerank_score" in h.get("ranking", {}) for h in hits):
        hits = sorted(hits, key=lambda h: definition_rank(question, h["quote"]), reverse=True)
    terms = [features(h["quote"]) for h in hits]
    query = features(question)
    frequency = Counter(t for row in terms for t in row)
    weights = {t: math.log(1 + (len(hits) + 1) / (frequency[t] + 1)) for t in query}
    total_weight = sum(weights.values()) or 1
    definitions = [definition_rank(question, h["quote"]) for h in hits]
    learned = any("rerank_score" in h.get("ranking", {}) for h in hits)
    selected, covered, remaining, size = [], set(), list(range(len(hits))), 0
    while remaining and len(selected) < limit:
        scored = []
        for index in remaining:
            hit = hits[index]
            if size + len(hit["quote"]) > char_budget:
                continue
            native = hit.get("ranking", {}).get("rerank_score")
            relevance = native if native is not None else 0 if learned else 1 / (1 + index / 6)
            gain = sum(weights[t] for t in (query & terms[index]) - covered) / total_weight
            redundancy = max((len(terms[index] & terms[j]) / max(len(terms[index] | terms[j]), 1)
                              for j in selected), default=0)
            utility = .75 * relevance + .15 * definitions[index] + .10 * gain - .30 * redundancy
            scored.append((utility, -index, index))
        if not scored:
            break
        index = max(scored)[2]
        selected.append(index)
        covered.update(query & terms[index])
        size += len(hits[index]["quote"])
        remaining.remove(index)
    return [hits[i] for i in selected]
