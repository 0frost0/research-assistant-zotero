"""Versioned post-retrieval policy; keeps embeddings and canonical evidence IDs intact."""

from __future__ import annotations

import json
from dataclasses import asdict


POLICY_VERSION = "research-context-v2"
CANDIDATE_K = 40
CONTEXT_LIMIT = 1800
NOISE_TYPES = {"header", "footer", "page_number"}
VISUAL_TYPES = {"image", "chart"}
VISUAL_WARNING = "图表自动转写未经核验，不可据此推断精确数值或连线；回答模型未直接查看图片。"
TABLE_WARNING = "表格来自自动提取，单元格数值与单位未经独立核验。"


def policy_settings():
    return {"version": POLICY_VERSION, "per_source_cap": None,
            "excluded_types": sorted(NOISE_TYPES), "exclude_visual_transcripts": True,
            "context_limit": CONTEXT_LIMIT, "deduplicate_identical_context": True,
            "candidate_k": CANDIDATE_K}


def _bbox_key(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _text_key(source, page, bbox, text):
    return source, page, _bbox_key(bbox), text


def _document_key(metadata):
    return json.dumps({k: v for k, v in metadata.items() if k != "start_index"},
                      sort_keys=True, ensure_ascii=False)


class EvidenceContext:
    """Resolve an exact retrieved slice back to its source block, never by ordinal alone."""

    def __init__(self, documents, chunks):
        self.documents = {_document_key(d.metadata): d for d in documents}
        self.matches = {}
        for chunk in chunks:
            m = chunk.metadata
            key = _text_key(m["source"], m.get("page"), m.get("bbox"), chunk.page_content)
            reference = (_document_key(m), int(m.get("start_index", 0)))
            self.matches.setdefault(key, set()).add(reference)

    def expand(self, hit):
        text = hit["text"]
        matches = self.matches.get(_text_key(hit["source"], hit.get("page"), hit.get("bbox"), text), set())
        if len(matches) != 1:
            return text, None
        document_key, anchor = next(iter(matches))
        document = self.documents.get(document_key)
        if document is None or document.page_content[anchor:anchor + len(text)] != text:
            return text, None
        full = document.page_content
        if len(full) <= CONTEXT_LIMIT:
            start, end = 0, len(full)
        else:
            start = max(0, anchor - 300)
            end = min(len(full), max(start + CONTEXT_LIMIT, anchor + len(text)))
        return full[start:end], {"source": hit["source"], "page": hit.get("page"),
            "block_index": document.metadata.get("block_index"), "bbox": hit.get("bbox"),
            "start": start, "end": end, "anchor_start": anchor,
            "complete_block": start == 0 and end == len(full)}


def select_evidence(ranking, threshold, *, limit=10, context=None):
    """Same selection/materialization for the production search and fixed benchmark."""
    result, seen = [], {}
    for value in ranking:
        hit = dict(value) if isinstance(value, dict) else asdict(value)
        kind = hit.get("content_type", "text")
        visual = hit.get("modality") == "image"
        if hit["score"] < threshold or kind in NOISE_TYPES:
            continue
        # Graphs and digitized charts are not reliable text passages. Keep the image point instead.
        if not visual and kind in VISUAL_TYPES:
            continue
        original = hit.get("text", "")
        warnings = []
        if visual:
            quote, span = "", None
            warnings.append(VISUAL_WARNING)
            if kind == "table":
                warnings.append(TABLE_WARNING)
            key = (hit["source"], "image", hit.get("point_id"))
        else:
            quote, span = context.expand(hit) if context else (original, None)
            if kind == "table":
                warnings.append(TABLE_WARNING)
            if span and not span["complete_block"]:
                warnings.append("原始段落过长，当前上下文仍是有界片段，请结合原文核对。")
            key = (hit["source"], hit.get("page"), "text", kind, _bbox_key(hit.get("bbox")),
                   span.get("block_index") if span else None, quote)
        if key in seen:
            # Only group IDs whose exact source context is actually present in this result.
            ids = seen[key]["context_evidence_ids"]
            if hit["point_id"] not in ids:
                ids.append(hit["point_id"])
            continue
        if len(result) < limit:
            item = {**hit, "retrieved_text": original, "text": quote,
                    "context_span": span, "quality_warnings": warnings,
                    "context_evidence_ids": [hit["point_id"]], "retrieval_policy": POLICY_VERSION}
            seen[key] = item
            result.append(item)
    return result
