"""Reuse the existing retrieval engine without syncing a per-collection subset."""
import json
import threading
import hashlib
import time
from collections import OrderedDict
from contextvars import copy_context
from research_assistant.core.service_context import service_context, embedding_config
from urllib.parse import urlencode

from langchain_core.documents import Document
from research_assistant.retrieval.knowledge_base import LiteratureIndex
from research_assistant.knowledge.store import digest, text


def note_source(ident, revision):
    return f"notes/{ident}-{revision}.md"


def zotero_url(library, item_key, page=None):
    prefix = "groups/" + library.split(":", 1)[1] if library.startswith("group:") else "library"
    url = f"zotero://open-pdf/{prefix}/items/{item_key}"
    return url + "?" + urlencode({"page": page or 1})


class CachedIndex(LiteratureIndex):
    def __init__(self, home, documents, backend, scope="all"):
        self.documents = documents
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
        if backend not in {"tfidf", "multimodal"}:
            raise ValueError("知识库仅支持 tfidf 或 multimodal。")
        self.store, self.backend = store, backend
        self.lock = threading.RLock()
        self.signature, self.index = None, None
        self._indexes = OrderedDict()
        self._building = set()
        self._failures = {}

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
                    hits = index.search_results(query, sources=list(sources), k=count)
                    if index.unified_error:
                        warnings.append("语义检索不可用，当前使用关键词检索。")
                    effective = "tfidf" if index.unified_error else self.backend
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
