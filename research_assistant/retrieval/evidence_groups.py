"""Deterministic links between text and visual retrieval evidence.

The vector index deliberately stores atomic text and image points.  Answering,
however, needs the small unit a researcher sees: a figure/table plus its
caption and nearby explanation.  This module adds that relationship after
retrieval without changing point IDs or pretending that a caption is an
observation of pixels.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict


def _bbox(value):
    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return None
    try:
        left, top, right, bottom = (float(item) for item in value[:4])
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (left, top, right, bottom)):
        return None
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top
    return left, top, right, bottom


def _near_or_overlapping(first, second, margin=80.0):
    a, b = _bbox(first), _bbox(second)
    if not a or not b:
        return False
    return not (
        a[2] + margin < b[0]
        or b[2] + margin < a[0]
        or a[3] + margin < b[1]
        or b[3] + margin < a[1]
    )


def _near(first, second):
    # MinerU variants use different coordinate systems. Unknown units must not
    # turn an arbitrary 80-unit margin into a claimed caption/explanation link.
    space = first.get("bbox_coordinate_space")
    if space not in {"pdf_points", "mineru_1000"} or second.get("bbox_coordinate_space") != space:
        return False
    return _near_or_overlapping(first.get("bbox"), second.get("bbox"))


def _group_id(source, page, members):
    raw = json.dumps([source, page, sorted(members)], ensure_ascii=False)
    return "G" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def attach_evidence_groups(results: list[dict]) -> list[dict]:
    """Annotate result dictionaries with stable page/visual relationships.

    Grouping is intentionally conservative: candidates must belong to the same
    source and page.  A bbox relation is recorded only when both boxes are
    available and close; otherwise ``same_page`` remains an honest weaker link.
    The function never changes ranking, quotes, or evidence IDs.
    """
    by_page: dict[tuple[str, object], list[dict]] = defaultdict(list)
    for item in results:
        source = str(item.get("source_id") or item.get("source") or "")
        version = item.get("document_version") or item.get("visual_asset", {}).get("document_version") or item.get("revision")
        by_page[(source, version, item.get("page"))].append(item)

    for (source, version, page), items in by_page.items():
        members = [str(item.get("retrieval_evidence_id") or item.get("evidence_id") or "")
                   for item in items]
        group_id = _group_id([source, version], page, members)
        for item in items:
            item["evidence_group_id"] = group_id
            item["linked_evidence_ids"] = [member for member in members
                                            if member and member != str(item.get("retrieval_evidence_id")
                                                                         or item.get("evidence_id") or "")]
            relations = []
            if item.get("modality") == "image":
                for other in items:
                    if other is item or other.get("modality") == "image":
                        continue
                    relation = "nearby_text" if _near(item, other) else "same_page"
                    relations.append({"type": relation,
                                      "evidence_id": other.get("retrieval_evidence_id") or other.get("evidence_id")})
            elif any(other.get("modality") == "image" for other in items):
                for other in items:
                    if other.get("modality") == "image":
                        relation = "nearby_visual" if _near(item, other) else "same_page"
                        relations.append({"type": relation,
                                          "evidence_id": other.get("retrieval_evidence_id") or other.get("evidence_id")})
            item["evidence_relations"] = relations
    return results
