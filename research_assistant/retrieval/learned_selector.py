"""Experimental offline selector, deliberately not wired to production QA."""
import math

from research_assistant.retrieval.selector_dataset import render_prompt


def greedy_select(candidates, question, scorer, *, limit=6, char_budget=12000):
    """No gold metadata reaches scorer. Never truncate evidence or invent hits."""
    if len({c["id"] for c in candidates}) != len(candidates):
        raise ValueError("duplicate candidate IDs")
    selected, size = [], 0
    remaining = list(candidates)
    while remaining and len(selected) < min(limit, 6):
        eligible = [c for c in remaining if size + len(c["quote"]) <= char_budget]
        if not eligible:
            break
        scores = scorer([render_prompt(question, selected, c) for c in eligible])
        if (len(scores) != len(eligible) or
                any(type(s) not in (int, float) or not math.isfinite(s) or not 0 <= s <= 1 for s in scores)):
            raise ValueError("invalid selector scores; offline evaluation fails closed")
        index = max(range(len(scores)), key=lambda i: scores[i])
        # Scores are not calibrated sufficiency probabilities. Match fixed top-k
        # budget, without inventing a stopping threshold from this development set.
        chosen = eligible[index]
        selected.append(chosen)
        size += len(chosen["quote"])
        remaining = [c for c in remaining if c["id"] != chosen["id"]]
    return selected
