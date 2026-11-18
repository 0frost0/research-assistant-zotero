"""SetR-style set generation and MultiHopRAG evidence-union metrics.

An adaptation, not the original Llama/GPT-4o checkpoint or exact retriever.
Gold spans are evaluator-only. Unannotated passages are not semantic negatives.
"""
import hashlib
import json
import re
from statistics import mean


PROTOCOL = "setr-multihoprag-evidence-union-v1"
SYSTEM = "You are RankLLM, an intelligent assistant that can rank and select passages based on their relevancy to the query."
# IRI prompt from LGAI-Research/SetR generate_data.py at d1ac78b (BSD-3-Clause).
# Copyright (c) 2025 the LG AI Research. All rights reserved.
OFFICIAL_IRI = """I will provide you with {num} passages, each indicated by a numerical identifier []. Select the passages based on their relevance to the search query: {question}.

{context}


Search Query: {question}


Please follow the steps below:
Step 1. Please list up the information requirements to answer the query.
Step 2. for each requirement in Step 1, find the passages that has the information of the requirement.
Step 3. Choose the passages that mostly covers clear and diverse informations to answer the query. Number of passages is unlimited. The format of final output should be '### Final Selection: [] []', e.g., ### Final Selection: [4] [2]."""
INTRO = "I will provide you with {num} passages, each indicated by a numerical identifier []. Select the passages based on their relevance to the search query: {question}.\n\n{context}\n\nSearch Query: {question}\n\n"
SELECTION = ("Select the passages that mostly covers clear and diverse informations to answer the query. "
             "Number of passages is unlimited. The format of final output should be '### Final Selection: [] []', "
             "e.g., ### Final Selection: [2] [1]. Only respond with the selection results, do not say any word or explain.")
RANK = ("Rank the {num} passages above based on their relevance to the search query. All the passages should be "
        "included and listed using identifiers, in descending order of relevance. The output format should be "
        "[] > [], e.g., [4] > [2]. Only respond with the ranking results, do not say any word or explain.")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def clean(text):
    # Fail rather than silently score with a different normalization protocol.
    from ftfy import fix_text
    return fix_text(text)


def normalize(text):
    # Same rule for raw gold and candidate text. Do not lowercase, stem, fuzzy
    # match, or infer paraphrases. Literal containment after whitespace removal.
    return "".join(clean(text).split())


def messages(question, candidates, mode="iri"):
    if mode not in {"iri", "selection", "rank"}:
        raise ValueError("unknown SetR prompt mode")
    # Whitelist only text/question; IDs, labels, facts, scores never enter prompt.
    passages = [re.sub(r"\[(\d+)\]", r"(\1)", clean(c["text"])) for c in candidates]
    context = "\n\n\n".join(f"[{i}] {p}" for i, p in enumerate(passages, 1))
    if mode == "iri":
        return [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": OFFICIAL_IRI.format(num=len(candidates), question=clean(question), context=context)}]
    intro = INTRO.format(num=len(candidates), question=clean(question), context=context)
    suffix = SELECTION if mode == "selection" else RANK.format(num=len(candidates))
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": intro + suffix}]


def parse_selection(text, count, *, ranking=False):
    """No silent padding, clipping, duplicate removal, repair or top-k fallback."""
    if ranking:
        body = text.strip()
        if not re.fullmatch(r"\[\d+\](?:\s*>\s*\[\d+\])*", body):
            raise ValueError("malformed ranking")
    else:
        markers = list(re.finditer(r"###\s*Final Selection\s*:", text, flags=re.I))
        if len(markers) != 1:
            raise ValueError("missing or repeated final selection marker")
        body = text[markers[0].end():].strip()
        if body == "[]":
            return []
        if not re.fullmatch(r"\[\d+\](?:\s+\[\d+\])*", body):
            raise ValueError("malformed final selection")
    indices = [int(x) - 1 for x in re.findall(r"\[(\d+)\]", body)]
    if any(i < 0 or i >= count for i in indices) or len(set(indices)) != len(indices):
        raise ValueError("out-of-range or duplicate selection")
    if ranking and len(indices) != count:
        raise ValueError("incomplete ranking")
    return indices


def evidence_sets(candidates, gold, *, normalized_texts=None):
    facts = list(dict.fromkeys(normalize(e["fact"]) for e in gold))
    if any(not f for f in facts):
        raise ValueError("empty evidence span")
    texts = normalized_texts if normalized_texts is not None else [normalize(c["text"]) for c in candidates]
    if len(texts) != len(candidates):
        raise ValueError("normalized candidate count mismatch")
    return facts, [{i for i, fact in enumerate(facts) if fact in text} for text in texts]


def metrics(candidates, gold, selected_indices, *, matched_evidence=None):
    if (len(set(selected_indices)) != len(selected_indices) or
            any(type(i) is not int or i < 0 or i >= len(candidates) for i in selected_indices)):
        raise ValueError("invalid evaluated selection")
    facts, matches = matched_evidence if matched_evidence is not None else evidence_sets(candidates, gold)
    pool = set().union(*matches) if matches else set()
    seen, increments, redundant = set(), [], 0
    for i in selected_indices:
        increments.append(len(matches[i] - seen))
        redundant += int(bool(matches[i]) and not (matches[i] - seen))
        seen.update(matches[i])
    n = len(facts)
    result = {"gold_count": n, "pool_gold_count": len(pool), "covered_gold_count": len(seen),
              "candidate_ic": len(pool) / n if n else None,
              "ic": len(seen) / n if n else None,
              "available_ic": len(seen) / len(pool) if pool else None,
              "all_gold": int(len(seen) == n) if n else None,
              "hit": int(bool(seen)) if n else None,
              "annotated_passage_precision": mean(bool(matches[i]) for i in selected_indices) if selected_indices and n else (0 if n else None),
              "redundant_gold_passages": redundant, "selected_count": len(selected_indices),
              "selected_chars": sum(len(candidates[i]["text"]) for i in selected_indices),
              "new_gold_per_passage": increments, "covered_gold_indices": sorted(seen)}
    for k in (1, 3, 5, 6):
        covered = set().union(*(matches[i] for i in selected_indices[:k])) if selected_indices else set()
        result[f"ic_at_{k}"] = len(covered) / n if n else None
    return result


def summarize(rows):
    keys = ("candidate_ic", "ic", "available_ic", "all_gold", "hit", "annotated_passage_precision",
            "redundant_gold_passages", "selected_count", "selected_chars", "ic_at_1", "ic_at_3", "ic_at_5", "ic_at_6")
    return {"questions": len(rows), "answerable": sum(r["gold_count"] > 0 for r in rows),
            "invalid_outputs": sum(r.get("status") != "ok" for r in rows),
            **{key: mean(r[key] for r in rows if r[key] is not None) if any(r[key] is not None for r in rows) else None for key in keys},
            "null_questions": sum(r["gold_count"] == 0 for r in rows),
            "null_empty_selection_rate": mean(r.get("status") == "ok" and r["selected_count"] == 0 for r in rows if r["gold_count"] == 0) if any(r["gold_count"] == 0 for r in rows) else None}
