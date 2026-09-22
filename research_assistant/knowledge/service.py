"""Reuse the existing retrieval engine without syncing a per-collection subset."""
import json
import threading
from urllib.parse import urlencode

from langchain_core.documents import Document
from research_assistant.retrieval.knowledge_base import LiteratureIndex
from research_assistant.knowledge.store import digest, encoded, text


def note_source(ident, revision):
    return f"notes/{ident}-{revision}.md"


def zotero_url(library, item_key, page=None):
    prefix = "groups/" + library.split(":", 1)[1] if library.startswith("group:") else "library"
    url = f"zotero://open-pdf/{prefix}/items/{item_key}"
    return url + "?" + urlencode({"page": page or 1})


class CachedIndex(LiteratureIndex):
    def __init__(self, home, documents, backend):
        self.documents = documents
        namespace = "research_zotero_v1_" + digest(str(home.resolve()).encode())[:12]
        super().__init__(home / "objects", backend=backend, collection_base=namespace)

    def _load_documents(self):
        return self.documents


class KnowledgeService:
    def __init__(self, store, backend="tfidf"):
        if backend not in {"tfidf", "multimodal"}:
            raise ValueError("知识库仅支持 tfidf 或 multimodal。")
        self.store, self.backend = store, backend
        self.lock = threading.Lock()
        self.signature, self.index = None, None

    def get_index(self):
        objects, entities, notes = self.store.corpus()
        signature = digest(encoded([(e["id"], e["revision"]) for e in entities] +
                                   [(n["id"], n["revision"]) for n in notes]).encode())
        if self.signature == signature:
            return self.index
        documents, seen = [], set()
        for entity in entities:
            value = json.loads(entity["payload"])
            if value["kind"] == "document":
                sha = value["hash"]
                if sha in seen:
                    continue
                seen.add(sha)
                for page in objects[sha]:
                    if page["text"].strip():
                        documents.append(Document(page_content=page["text"], metadata={
                            "source": sha + ".pdf", "file_type": ".pdf", "page": page["page"],
                        }))
            elif value.get("confirmed"):
                body = value["body"] + "\n" + value.get("comment", "")
                if body.strip():
                    documents.append(Document(page_content=body, metadata={
                        "source": note_source(entity["id"], entity["revision"]),
                        "file_type": ".md", "page": None,
                    }))
        for note in notes:
            value = json.loads(note["payload"])
            if value["confirmed"] and not value["archived"]:
                documents.append(Document(page_content=value["body"], metadata={
                    "source": note_source(note["id"], note["revision"]),
                    "file_type": ".md", "page": None,
                }))
        self.index = CachedIndex(self.store.home, documents, self.backend) if documents else None
        self.signature = signature
        return self.index

    def search(self, data):
        query = text(data.get("query"), "检索问题", 2000)
        scope = data.get("scope", "both")
        if scope not in {"both", "original", "notes"}:
            raise ValueError("检索范围无效。")
        count = data.get("k", 8)
        if type(count) is not int or not 1 <= count <= 20:
            raise ValueError("结果数应为 1–20。")
        snapshot = self.store.snapshot(data.get("kb_ids"))
        sources = {}
        for entity in snapshot["entities"]:
            value = entity["data"]
            if value["kind"] == "document" and scope != "notes":
                source = value["hash"] + ".pdf"
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
                }
        if scope != "original":
            for note in snapshot["notes"]:
                value = note["data"]
                if value["confirmed"] and not value["archived"]:
                    sources[note_source(note["id"], note["revision"])] = {
                        "kind": "user_note", "title": value["title"], "entity_id": note["id"],
                        "revision": note["revision"], "kb_id": note["kb_id"],
                    }
        unreadable = any(e["data"].get("extraction", {}).get("empty_pages", 0) or
                         e["data"].get("extraction", {}).get("low_text_pages", 0) for e in snapshot["entities"])
        warnings = ["部分 PDF 页面缺少足够文字；此版本未执行 OCR，检索覆盖不完整。"] if unreadable else []
        results, effective = [], self.backend
        if sources:
            # Serialize rebuild/query so a simultaneous sync cannot swap the shared index.
            with self.lock:
                index = self.get_index()
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
                                        "source_id": hit.source})
        self.store.assert_current(snapshot)
        return {"query": query, "scope": scope, "backend": effective, "warning": " ".join(warnings) or None,
                "results": results, "revisions": {b["id"]: b["revision"] for b in snapshot["bases"]}}
