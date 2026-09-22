"""Authenticated Zotero bridge. No automatic translation or model generation."""
import json
import os
from pathlib import Path
import secrets
import tempfile
from urllib.parse import urlsplit

from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from research_assistant.knowledge.store import KnowledgeStore
from research_assistant.knowledge.service import KnowledgeService
from research_assistant.knowledge.backup import export_backup
from research_assistant.memory.store import Conflict
from research_assistant.core.paths import STATIC_DIR


class KnowledgeAPI:
    def __init__(self, root, token=None, backend=None):
        self.root = Path(root)
        self.explicit_token = token
        self.backend = backend or os.getenv("RESEARCH_KNOWLEDGE_BACKEND", "multimodal")
        self._store = self._service = None

    @property
    def store(self):
        if self._store is None:
            self._store = KnowledgeStore(self.root)
        return self._store

    @property
    def service(self):
        if self._service is None:
            self._service = KnowledgeService(self.store, self.backend)
        return self._service

    def token(self):
        if self.explicit_token is not None:
            return self.explicit_token
        configured = os.getenv("RESEARCH_ZOTERO_TOKEN")
        if configured:
            return configured
        path = self.root / ".data/zotero/connection-token"
        return path.read_text().strip() if path.is_file() else None

    async def endpoint(self, request):
        configured = self.token()
        if not configured:
            return JSONResponse({"error": "Zotero 连接尚未配置。"}, status_code=503)
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied, "Bearer " + configured):
            return JSONResponse({"error": "连接令牌无效。"}, status_code=401)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            return JSONResponse({"error": "拒绝跨站请求。"}, status_code=403)
        action, ident = request.path_params.get("action"), request.path_params.get("ident")
        try:
            if request.method == "GET":
                if action == "status":
                    result = {"protocol": 1, "backend": self.backend, "generation": False}
                elif action == "bases":
                    result = self.store.snapshot([ident]) if ident else {"bases": self.store.bases()}
                elif action == "objects" and ident:
                    result = {"hash": ident, "exists": await run_in_threadpool(self.store.object_exists, ident)}
                elif action == "export":
                    fd, name = tempfile.mkstemp(suffix=".zip")
                    os.close(fd)
                    try:
                        await run_in_threadpool(export_backup, self.store, name)
                    except BaseException:
                        Path(name).unlink(missing_ok=True)
                        raise
                    return FileResponse(name, media_type="application/zip",
                                        filename="research-knowledge-backup.zip",
                                        background=BackgroundTask(Path(name).unlink, missing_ok=True))
                else:
                    raise KeyError(action)
            else:
                limit = 25 * 1024 * 1024 if action == "objects" else 8 * 1024 * 1024
                raw = bytearray()
                async for part in request.stream():
                    raw.extend(part)
                    if len(raw) > limit:
                        raise ValueError("请求超过允许大小。")
                if action == "objects" and ident and request.method == "PUT":
                    result = await run_in_threadpool(self.store.put_object, ident, bytes(raw))
                else:
                    data = json.loads(raw or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError("请求须为 JSON 对象。")
                    if action == "bases" and not ident:
                        result = self.store.create(data)
                    elif action == "sync" and ident:
                        if data.get("allow_upload") is not True:
                            raise ValueError("同步前需要明确允许将所选内容发送到后端。")
                        result = await run_in_threadpool(self.store.sync, ident, data)
                    elif action == "search":
                        result = await run_in_threadpool(self.service.search, data)
                    elif action == "notes":
                        result = self.store.save_note(data.get("kb_id"), data, ident)
                    elif action == "archive" and ident:
                        result = self.store.set_archive(ident, data)
                    else:
                        raise KeyError(action)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except Conflict as exc:
            return JSONResponse({"error": str(exc), "code": "revision_conflict"}, status_code=409)
        except KeyError:
            return JSONResponse({"error": "知识库或记录不存在。"}, status_code=404)
        except (ValueError, TypeError, UnicodeError):
            return JSONResponse({"error": "请求无效、文件损坏或同步资料不完整；已有数据未被清空。"}, status_code=400)
        except Exception:
            return JSONResponse({"error": "知识库服务暂不可用，已有资料保留。"}, status_code=503)

    async def page(self, request):
        return FileResponse(STATIC_DIR / "knowledge.html",
                            headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})

    def routes(self):
        return [Route("/knowledge", self.page)] + [
            Route("/api/knowledge/{action}" + suffix, self.endpoint, methods=["GET", "POST", "PUT"])
            for suffix in ("", "/{ident}")
        ]
