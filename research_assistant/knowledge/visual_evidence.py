"""Scoped, bounded visual assets; never accept a client-supplied filesystem path.

MinerU paths are private implementation details.  Public locators are hashes of
document content, parser representation, location and original image bytes.
Rasterized PDF pages are an explicit fallback when no crop exists.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
from pathlib import Path

from PIL import Image


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_OUTPUT_EDGE = 1600


def identifier(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class VisualEvidenceRepository:
    def __init__(self, store):
        self.store = store

    def _sources(self, kb_ids):
        if (not isinstance(kb_ids, list) or not 1 <= len(kb_ids) <= 20
                or not all(isinstance(k, str) and k for k in kb_ids)):
            raise ValueError("请明确选择知识库。")
        snapshot = self.store.snapshot(kb_ids)
        sources = {e["data"]["hash"] + ".pdf" for e in snapshot["entities"]
                   if e["data"]["kind"] == "document"}
        return snapshot, sources

    def _safe_path(self, value):
        if not value:
            raise ValueError("图像资产缺失。")
        path = Path(value).resolve()
        # Both parser cache and page fallback are project-owned derived data.
        if not any(path.is_relative_to((self.store.home / name).resolve())
                   for name in ("mineru", "visual-pages")) or not path.is_file():
            raise ValueError("图像资产不在当前知识库缓存内。")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("图像资产过大。")
        return path

    def catalog(self, kb_ids):
        snapshot, allowed = self._sources(kb_ids)
        objects, _, _ = self.store.corpus()
        entries = []
        for source in sorted(allowed):
            blocks = objects.get(source[:-4], [])
            # Paths differ after moving a workspace; they must not define identity.
            version = identifier([{ "text": b["text"], "page": b.get("page"),
                "metadata": {k: v for k, v in b.get("metadata", {}).items() if k != "image_path"}}
                for b in blocks])
            seen = set()
            for block in blocks:
                meta = block.get("metadata", {})
                if not meta.get("image_path"):
                    continue
                try:
                    path = self._safe_path(meta["image_path"])
                    raw_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                except (OSError, ValueError):
                    continue
                key = (block.get("page"), raw_hash, json.dumps(meta.get("bbox")))
                if key in seen:
                    continue
                seen.add(key)
                asset_id = "V" + identifier([source, version, *key])[:32]
                caption = block["text"] if block["text"] != "[图像；无文字图注]" else ""
                entries.append({"asset_id": asset_id, "source_id": source,
                    "document_version": version, "page": block.get("page"),
                    "bbox": meta.get("bbox"), "caption": caption[:1800],
                    "caption_truncated": len(caption) > 1800,
                    "content_type": meta.get("content_type", "image"),
                    "image_hash": raw_hash, "_path": str(path), "asset_type": "parser_crop"})
        self.store.assert_current(snapshot)
        return entries

    @staticmethod
    def public(entry):
        return {k: v for k, v in entry.items() if not k.startswith("_")}

    def matches_path(self, entry, value):
        try:
            return self._safe_path(value) == self._safe_path(entry["_path"])
        except (OSError, ValueError):
            return False

    def matches_identity(self, entry, identity):
        return bool(identity and identity["source"] == entry["source_id"]
                    and identity["page"] == entry["page"]
                    and json.loads(identity["bbox_json"] or "null") == entry["bbox"]
                    and identity["image_hash"] == entry["image_hash"]
                    and self.matches_path(entry, identity["image_path"]))

    def resolve(self, kb_ids, asset_id):
        if not isinstance(asset_id, str):
            raise ValueError("图像标识无效。")
        match = re.fullmatch(r"P([0-9a-f]{64})-([1-9][0-9]{0,5})-([0-9a-f]{32})", asset_id)
        if match:
            snapshot, allowed = self._sources(kb_ids)
            sha, page, hash_prefix = match.groups()
            source = sha + ".pdf"
            if source not in allowed:
                raise KeyError(asset_id)
            path = self._safe_path(self.store.home / "visual-pages" / (asset_id + ".png"))
            image_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if not image_hash.startswith(hash_prefix):
                raise ValueError("页面图像校验失败。")
            self.store.assert_current(snapshot)
            return self._page_entry(asset_id, source, int(page), image_hash, path)
        entries = self.catalog(kb_ids)
        entry = next((item for item in entries if item["asset_id"] == asset_id), None)
        if entry is None:
            raise KeyError(asset_id)
        return entry

    def encode(self, kb_ids, asset_id):
        snapshot, _ = self._sources(kb_ids)
        entry = self.resolve(kb_ids, asset_id)
        result = self._encode_entry(entry)
        self.store.assert_current(snapshot)
        return result

    def _encode_entry(self, entry):
        raw = self._safe_path(entry["_path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["image_hash"]:
            raise ValueError("图像资产版本已改变，请重新检索。")
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("图像像素超过上限。")
            image.load()
            image = image.convert("RGB")
            image.thumbnail((MAX_OUTPUT_EDGE, MAX_OUTPUT_EDGE))
            output = io.BytesIO()
            image.save(output, format="PNG")
        content = output.getvalue()
        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError("图像编码超过上限。")
        return {**self.public(entry), "media_type": "image/png",
                "data_url": "data:image/png;base64," + base64.b64encode(content).decode("ascii")}

    def render_page(self, kb_ids, source, page):
        snapshot, allowed = self._sources(kb_ids)
        if source not in allowed or type(page) is not int or page < 1:
            raise ValueError("页面不属于所选知识库。")
        try:
            import fitz
        except ImportError as exc:
            raise ValueError("PDF 页面回退需要安装可选光栅化依赖；当前环境未启用。") from exc
        pdf_path = self.store.objects / source
        if hashlib.sha256(pdf_path.read_bytes()).hexdigest() != source[:-4]:
            raise ValueError("PDF 内容校验失败。")
        with fitz.open(pdf_path) as document:
            if page > len(document):
                raise ValueError("页码超出文档范围。")
            rect = document[page - 1].rect
            scale = min(2.0, MAX_OUTPUT_EDGE / max(rect.width, rect.height))
            content = document[page - 1].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png")
        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError("页面图像过大。")
        image_hash = hashlib.sha256(content).hexdigest()
        asset_id = "P" + source[:-4] + "-" + str(page) + "-" + image_hash[:32]
        folder = self.store.home / "visual-pages"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (asset_id + ".png")
        path.write_bytes(content)
        self.store.assert_current(snapshot)
        result = self._encode_entry(self._page_entry(asset_id, source, page, image_hash, path))
        self.store.assert_current(snapshot)
        return result

    @staticmethod
    def _page_entry(asset_id, source, page, image_hash, path):
        return {"asset_id": asset_id, "source_id": source, "page": page,
            "bbox": None, "caption": "PDF 原始页面（包含未筛选上下文）", "content_type": "page",
            "document_version": source[:-4], "image_hash": image_hash,
            "asset_type": "pdf_page", "_path": str(path)}
