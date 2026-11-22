"""Reuse the existing retrieval engine without syncing a per-collection subset."""
import json
import threading
import hashlib
import re
import time
from collections import OrderedDict
from contextvars import copy_context
from research_assistant.core.service_context import service_context, embedding_config
from urllib.parse import urlencode

from langchain_core.documents import Document
from research_assistant.retrieval.knowledge_base import LiteratureIndex
from research_assistant.retrieval.embeddings.api_embeddings import EmbeddingServiceUnavailable
from research_assistant.knowledge.store import digest, text


def note_source(ident, revision):
    return f"notes/{ident}-{revision}.md"


def identifier_candidates(index, query, sources, count):
    """Recover scoped candidates for a cross-language rewrite after an empty lexical search."""
    if not re.search(r"[\u4e00-\u9fff]", query):
        return []
    boundary = r"(?<![A-Za-z0-9_-]){}(?![A-Za-z0-9_-])"
    identifiers = dict.fromkeys(re.findall(boundary.format(r"[A-Z][A-Z0-9]+(?:[-_][A-Z0-9]+)*"), query))
    if not identifiers:
        return []
    terms = [re.compile(boundary.format(re.escape(term)), re.I) for term in identifiers]
    pool = index.search_results(" ".join(identifiers), sources=list(sources), k=20, min_score=0)
    return [hit for hit in pool if all(term.search(hit.quote) for term in terms)][:count]


def zotero_url(library, item_key, page=None):
    prefix = "groups/" + library.split(":", 1)[1] if library.startswith("group:") else "library"
    url = f"zotero://open-pdf/{prefix}/items/{item_key}"
    return url + "?" + urlencode({"page": page or 1})


class CachedIndex(LiteratureIndex):
    def __init__(self, home, documents, backend, scope="all"):
        self.documents = documents
        self.notes_fallback = None
        self.embedding_cache_path = home / "indexes" / "embedding-cache.sqlite3"
        namespace = "research_zotero_v1_" + digest(str(home.resolve()).encode())[:12]
        context = service_context.get()
        if context and embedding_config():
            namespace += "_" + digest(context["connection"].encode())[:12]
        namespace += "_" + digest(scope.encode())[:12]
        # Each scope has its own evidence DB as sync removes missing sources.
        super().__init__(home / "indexes" / namespace / "objects", backend=backend, collection_base=namespace)

    def _load_documents(self):
        return self.documents


class KnowledgeService:
    def __init__(self, store, backend="tfidf"):
        if backend not in {"tfidf", "multimodal", "bge"}:
            raise ValueError("知识库仅支持 tfidf、multimodal 或本地 bge。")
        self.store, self.backend = store, backend
        self.lock = threading.RLock()
        self.signature, self.index = None, None
        self._indexes = OrderedDict()
        self._building = set()
        self._failures = {}
        from research_assistant.knowledge.visual_evidence import VisualEvidenceRepository
        self.visuals = VisualEvidenceRepository(store)

    def get_index(self, allowed_sources=None, *, background=False):
        # Cheap probe first: avoids loading every page text just to detect "nothing changed".
        signature = self.store.corpus_signature()
        scope = json.dumps(sorted(allowed_sources)) if allowed_sources is not None else "all"
        signature = (signature, scope)
        context = service_context.get()
        if context and embedding_config():
            # In-memory only; key rotation also invalidates the cached client.
            signature = (signature, context["connection"], hashlib.sha256(
                json.dumps(embedding_config(), sort_keys=True).encode()).hexdigest())
        with self.lock:
            cached = self._indexes.get(signature)
            if cached:
                index, created = cached
                # A transient failure must not poison this scope until data changes.
                if not index or not index.unified_error or time.monotonic() - created < 30:
                    self._indexes.move_to_end(signature)
                    self.signature, self.index = signature, index
                    return index
                del self._indexes[signature]
            failure = self._failures.get(signature)
            if failure and time.monotonic() - failure < 30:
                raise RuntimeError("索引准备失败，请检查 Qwen 和 Qdrant 连接，30 秒后可重试。")
            # One writer: a revision change during a build must not start another
            # sync against the same evidence DB / Qdrant collection.
            if self._building:
                raise IndexBuilding()
            if background and self.backend == "multimodal":
                self._building.add(signature)
                context = copy_context()
                threading.Thread(target=context.run, args=(self._build_background, signature, allowed_sources, scope),
                                 daemon=True, name="knowledge-index").start()
                raise IndexBuilding()
        return self._publish_index(signature, self._build_index(allowed_sources, scope))

    def _publish_index(self, signature, index):
        with self.lock:
            self._indexes[signature] = (index, time.monotonic())
            while len(self._indexes) > 8:
                self._indexes.popitem(last=False)
            self.signature, self.index = signature, index
        return index

    def _build_background(self, signature, allowed_sources, scope):
        try:
            self._publish_index(signature, self._build_index(allowed_sources, scope))
        except Exception:
            with self.lock:
                self._failures[signature] = time.monotonic()
        finally:
            with self.lock:
                self._building.discard(signature)

    def _build_index(self, allowed_sources, scope):
        objects, entities, notes = self.store.corpus()
        documents, seen = [], set()
        for entity in entities:
            value = json.loads(entity["payload"])
            if value["kind"] == "document":
                sha = value["hash"]
                if allowed_sources is not None and sha + ".pdf" not in allowed_sources:
                    continue
                if sha in seen:
                    continue
                seen.add(sha)
                for page in objects[sha]:
                    if page["text"].strip():
                        documents.append(Document(page_content=page["text"], metadata={
                            **page.get("metadata", {}),
                            "source": sha + ".pdf", "file_type": ".pdf", "page": page["page"],
                        }))
            elif value.get("confirmed"):
                if allowed_sources is not None and note_source(entity["id"], entity["revision"]) not in allowed_sources:
                    continue
                body = value["body"] + "\n" + value.get("comment", "")
                if body.strip():
                    documents.append(Document(page_content=body, metadata={
                        "source": note_source(entity["id"], entity["revision"]),
                        "file_type": ".md", "page": None,
                    }))
        for note in notes:
            if allowed_sources is not None and note_source(note["id"], note["revision"]) not in allowed_sources:
                continue
            value = json.loads(note["payload"])
            if value["confirmed"] and not value["archived"]:
                documents.append(Document(page_content=value["body"], metadata={
                    "source": note_source(note["id"], note["revision"]),
                    "file_type": ".md", "page": None,
                }))
        return CachedIndex(self.store.home, documents, self.backend, scope) if documents else None

    def search(self, data):
        query = text(data.get("query"), "检索问题", 2000)
        scope = data.get("scope", "both")
        if scope not in {"both", "original", "notes"}:
            raise ValueError("检索范围无效。")
        count = data.get("k", 8)
        if type(count) is not int or not 1 <= count <= 20:
            raise ValueError("结果数应为 1–20。")
        text_only = data.get("text_only", False)
        if type(text_only) is not bool:
            raise ValueError("文字检索选项必须是布尔值。")
        multimodal = data.get("multimodal", False)
        if type(multimodal) is not bool or (multimodal and (text_only or scope == "notes")):
            raise ValueError("图文证据仅适用于原文且不能同时只检索文字。")
        retrieval_mode = data.get("retrieval_mode", "legacy")
        if retrieval_mode not in {"legacy", "hybrid"} or (retrieval_mode == "hybrid" and not (text_only or multimodal)):
            raise ValueError("混合检索仅支持文字证据。")
        source_ids = data.get("source_ids")
        if source_ids is not None:
            if (not isinstance(source_ids, list) or not source_ids or len(source_ids) > 4
                    or not all(isinstance(value, str) and value.strip() for value in source_ids)
                    or len(set(source_ids)) != len(source_ids)):
                raise ValueError("来源筛选必须是 1–4 个不同的来源标识。")
            source_ids = set(source_ids)
        snapshot = self.store.snapshot(data.get("kb_ids"))
        sources = {}
        for entity in snapshot["entities"]:
            value = entity["data"]
            if value["kind"] == "document" and scope != "notes":
                source = value["hash"] + ".pdf"
                if source_ids is not None and source not in source_ids:
                    continue
                sources.setdefault(source, {"kind": "original", "title": value["title"],
                                            "item_key": value["key"], "library": entity["library"],
                                            "hash": value["hash"], "entity_id": entity["id"],
                                            "revision": entity["revision"]})
            elif value["kind"] != "document" and value.get("confirmed") and scope != "original":
                sources[note_source(entity["id"], entity["revision"])] = {
                    "kind": "external_" + value["kind"], "title": value["title"],
                    "entity_id": entity["id"], "revision": entity["revision"],
                    "item_key": value.get("attachment_key") or value["key"],
                    "library": entity["library"], "page": value.get("page"),
                    # Keep provenance explicit all the way to QA/UI.  An
                    # imported Zotero record is a user's external record,
                    # never a paper fact, even when its text resembles one.
                    "author_source": value.get("author_source", "external_zotero"),
                    "source_status": "external_zotero",
                }
        if scope != "original":
            for note in snapshot["notes"]:
                value = note["data"]
                if value["confirmed"] and not value["archived"]:
                    sources[note_source(note["id"], note["revision"])] = {
                        "kind": "user_note", "title": value["title"], "entity_id": note["id"],
                        "revision": note["revision"], "kb_id": note["kb_id"],
                        "anchors": value.get("anchors", []),
                        "source_status": value.get("source_status", "unlinked"),
                        "source_verified": bool(value.get("source_verified", False)),
                        "author_source": value.get("author_source", "user"),
                        "note_type": value.get("type", "understanding"),
                    }
        unreadable = any(e["data"].get("extraction", {}).get("empty_pages", 0) or
                         e["data"].get("extraction", {}).get("low_text_pages", 0) for e in snapshot["entities"])
        warnings = ["部分 PDF 页面缺少足够文字；此版本未执行 OCR，检索覆盖不完整。"] if unreadable else []
        results, effective = [], self.backend
        if sources:
            # Serialize rebuild/query so a simultaneous sync cannot swap the shared index.
            with self.lock:
                index = self.get_index(set(sources), background=True)
                if index is not None:
                    identifier_only = False
                    lexical = index if index.backend == "tfidf" or index.unified_error else None
                    try:
                        hits = index.search_results(
                            query, sources=list(sources), k=count,
                            **({"text_only": True} if text_only or (multimodal and retrieval_mode == "hybrid") else {}),
                            **({"retrieval_mode": retrieval_mode} if retrieval_mode != "legacy" else {}),
                        )
                    except EmbeddingServiceUnavailable:
                        if scope != "notes" or retrieval_mode != "legacy":
                            raise
                        # Tie the lexical index to the same snapshot and allowed note revisions.
                        if index.notes_fallback is None:
                            index.notes_fallback = CachedIndex(self.store.home, index.documents, "tfidf")
                        hits = index.notes_fallback.search_results(query, sources=list(sources), k=count)
                        lexical = index.notes_fallback
                        effective = "tfidf"
                        warnings.append("本次笔记查询无法访问编码服务，已使用关键词检索。")
                    if not hits and scope == "notes" and retrieval_mode == "legacy" and lexical is not None:
                        hits = identifier_candidates(lexical, query, sources, count)
                        if hits:
                            identifier_only = True
                            warnings.append("完整问题未命中；以下笔记仅精确匹配问题中的缩写，供核对。")
                    if multimodal and retrieval_mode == "hybrid" and index.unified_index is not None:
                        try:
                            visual_hits = index.unified_index.search(query, k=20, min_score=0.35,
                                sources=list(sources), modality="image")
                            hits += index.materialize_unified_results(visual_hits, k=8, min_score=0.35)
                        except Exception:
                            warnings.append("视觉召回不可用；保留文字证据，不计为已完成图像检索。")
                    if index.unified_error:
                        warnings.append("语义检索不可用，当前使用关键词检索。")
                        effective = "tfidf"
                    if retrieval_mode == "hybrid":
                        effective = hits[0].backend if hits else ("multimodal-hybrid" if index.unified_index is not None else "bm25-lexical")
                        if effective == "bm25-lexical-fallback":
                            warnings.append("本次混合检索仅完成 BM25 词面召回；未计为已完成向量召回。")
                    for hit in hits:
                        meta = sources.get(hit.source)
                        if not meta:
                            continue
                        page = hit.page or meta.get("page")
                        if meta["kind"] == "original":
                            url = zotero_url(meta["library"], meta["item_key"], page)
                        elif meta["kind"] == "external_annotation":
                            url = zotero_url(meta["library"], meta["item_key"], page)
                        elif meta.get("library"):
                            prefix = "groups/" + meta["library"].split(":", 1)[1] if meta["library"].startswith("group:") else "library"
                            url = f"zotero://select/{prefix}/items/{meta['item_key']}"
                        else:
                            url = "/knowledge?" + urlencode({"kb": meta["kb_id"], "note": meta["entity_id"]})
                        results.append({**meta, "page": page, "quote": hit.quote,
                                        "score": hit.score, "url": url,
                                        "retrieval_evidence_id": hit.evidence_id,
                                        "context_evidence_ids": hit.context_evidence_ids,
                                        "bbox": hit.bbox,
                                        "modality": hit.modality,
                                        "content_type": hit.content_type,
                                        "source_id": hit.source})
                        if getattr(hit, "ranking", None):
                            results[-1]["ranking"] = hit.ranking
                        if identifier_only:
                            results[-1]["identifier_only"] = True
                        if multimodal and hit.modality == "image":
                            results[-1]["visual_caption"] = hit.visual_caption or ""
                            results[-1]["_image_path"] = hit.image_path
                            identity = index.unified_index.store.active_visual_identity(hit.evidence_id) if index.unified_index else None
                            results[-1]["_visual_identity"] = identity
        if multimodal:
            catalog = self.visuals.catalog(data.get("kb_ids"))
            versions = {asset["source_id"]: asset["document_version"] for asset in catalog}
            for result in results:
                if result["source_id"] in versions:
                    result["document_version"] = versions[result["source_id"]]
                if result.get("modality") != "image":
                    continue
                # Identity must match source, location and canonical index image.
                candidates = [asset for asset in catalog if asset["source_id"] == result["source_id"]
                              and asset["page"] == result["page"] and asset["bbox"] == result["bbox"]
                              and self.visuals.matches_path(asset, result.get("_image_path"))
                              and self.visuals.matches_identity(asset, result.get("_visual_identity"))]
                if len(candidates) == 1:
                    result["visual_asset"] = self.visuals.public(candidates[0])
                result.pop("_image_path", None)
                result.pop("_visual_identity", None)
            results = [r for r in results if r.get("modality") != "image" or r.get("visual_asset")]
            # Reserve at most two slots for pixels and honour the public k bound.
            visuals = [r for r in results if r.get("modality") == "image"][:min(2, count)]
            results = [r for r in results if r.get("modality") != "image"][:count - len(visuals)] + visuals
            from research_assistant.retrieval.evidence_groups import attach_evidence_groups
            attach_evidence_groups(results)
        self.store.assert_current(snapshot)
        return {"query": query, "scope": scope, "backend": effective, "warning": " ".join(warnings) or None,
                "results": results, "revisions": {b["id"]: b["revision"] for b in snapshot["bases"]}}


class IndexBuilding(RuntimeError):
    def __init__(self):
        super().__init__("正在准备语义索引（首次建库或资料更新），完成后自动检索；无需重复提交。")


async def search_ready(service, data):
    """Wait without tying up HTTP threads or starting duplicate builds."""
    import asyncio
    for attempt in range(120):
        try:
            return await asyncio.to_thread(service.search, data)
        except IndexBuilding:
            if attempt == 119:
                raise
            await asyncio.sleep(1)
