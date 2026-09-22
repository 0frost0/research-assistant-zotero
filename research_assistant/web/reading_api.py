"""Reading routes independent of external embedding/model availability."""

import asyncio
import json
from contextlib import asynccontextmanager
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from research_assistant.memory.store import ReadingStore, Conflict
from research_assistant.memory.qa import answer, draft
from research_assistant.translation.service import TranslationService
from research_assistant.translation.selection import SelectionTranslator
from research_assistant.translation.alignment import AlignmentService


class ReadingAPI:
    def __init__(self, root, get_index=None):
        self.root = Path(root)
        self._store = None
        self._translator = None
        self._selection_translator = None
        self._alignment = None
        self.get_index = get_index

    @property
    def store(self):
        if self._store is None:
            self._store = ReadingStore(self.root)
        return self._store

    @property
    def translator(self):
        if self._translator is None:
            self._translator = TranslationService(self.store)
        return self._translator

    async def start(self):
        self.selection_translator.recover()
        await self.translator.start()

    @property
    def selection_translator(self):
        if self._selection_translator is None:
            self._selection_translator = SelectionTranslator(self.store)
        return self._selection_translator

    async def stop(self):
        await self.translator.stop()

    @property
    def alignment(self):
        if self._alignment is None:
            self._alignment = AlignmentService(self.store, Path(__file__).resolve().parents[2])
        return self._alignment

    @asynccontextmanager
    async def lifespan(self, app):
        await self.start()
        try:
            yield
        finally:
            await self.stop()

    @asynccontextmanager
    async def lifespan(self, app):
        await self.start()
        try:
            yield
        finally:
            await self.stop()

    def register(self, source):
        path = (self.root / "library" / source).resolve()
        if (
            not path.is_relative_to((self.root / "library").resolve())
            or path.suffix.lower() != ".pdf"
            or not path.is_file()
        ):
            raise ValueError("只能打开资料库内的 PDF。")
        return self.store.register(path, source)

    async def uploaded(self, path, source):
        paper = await run_in_threadpool(self.store.register, path, source)
        warning = None
        if self.translator.status()["configured"] and self.store.settings().get(
            "auto_translate", False
        ):
            original = self.store.artifact(paper["original_id"])
            if original["metadata"]["language"] == "en":
                try:
                    self.translator.submit(paper["id"])
                except ValueError as exc:
                    warning = str(exc)
        return {
            "paper_id": paper["id"],
            "reader_url": "/reader?paper=" + paper["id"],
            "translation_warning": warning,
        }

    async def endpoint(self, request):
        try:
            if request.method != "GET":
                origin = request.headers.get("origin")
                if origin and (
                    urlsplit(origin).netloc != request.url.netloc
                    or urlsplit(origin).scheme != request.url.scheme
                ):
                    return JSONResponse(
                        {"error": "仅接受本站发起的修改。"}, status_code=403
                    )
                raw = await request.body()
                if len(raw) > 256000:
                    raise ValueError("请求过大。")
                data = json.loads(raw or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("请求应为 JSON 对象。")
            else:
                data = {}
            action = request.path_params.get("action", "")
            ident = request.path_params.get("ident")
            if action == "papers":
                if request.method == "POST":
                    result = await run_in_threadpool(
                        self.register, str(data.get("source", ""))
                    )
                else:
                    # Existing originals are archived lazily. No model calls or automatic retranslations.
                    errors = []
                    for path in (self.root / "library").rglob("*.pdf"):
                        try:
                            await run_in_threadpool(
                                self.store.register,
                                path,
                                str(path.relative_to(self.root / "library")),
                            )
                        except Exception:
                            errors.append(
                                {
                                    "source": path.name,
                                    "error": "无法注册 PDF，可从原资料库下载检查。",
                                }
                            )
                    result = {"papers": self.store.papers(), "errors": errors}
            elif action == "settings":
                if request.method == "POST":
                    self.store.set_auto(data.get("auto_translate"))
                result = self.translator.status()
            elif action == "annotations":
                result = (
                    self.store.create_annotation(data)
                    if request.method == "POST"
                    else self.store.annotations(request.query_params.get("paper_id"))
                )
            elif action == "notes":
                result = (
                    self.store.save_note(data, ident)
                    if request.method == "POST"
                    else self.store.get(
                        ident,
                        int(request.query_params["revision"])
                        if "revision" in request.query_params
                        else None,
                    )
                    if ident
                    else self.store.notes(request.query_params.get("paper_id"))
                )
            elif action == "history":
                result = self.store.history(ident)
            elif action == "selection-translation":
                result = await run_in_threadpool(self.selection_translator.translate, data) if request.method == "POST" else self.selection_translator.status()
            elif action == "alignment":
                result = await run_in_threadpool(self.alignment.align, data) if request.method == "POST" else self.alignment.status()
            elif action == "selection-translation-draft" and request.method == "POST":
                result = self.selection_translator.draft(ident, data)
            elif action == "selection-history" and request.method == "GET":
                result = self.selection_translator.history(request.query_params.get("paper_id"))
            elif action == "search":
                result = self.store.search(data.get("query", ""), data.get("paper_id"))
            elif action == "jobs":
                if request.method == "POST":
                    job, created = self.translator.submit(
                        str(data.get("paper_id", "")),
                        force=data.get("force") is True,
                        manual=data.get("manual") is True,
                    )
                    result = {"job": job, "created": created}
                else:
                    result = self.translator.jobs(request.query_params.get("paper_id"))
            elif action == "retry":
                result = self.translator.retry(ident)
            elif action == "cancel" and request.method == "POST":
                result = await self.translator.cancel(ident)
            elif action == "ask":
                result = await asyncio.wait_for(
                    run_in_threadpool(answer, self.store, data, self.get_index), 75
                )
            elif action == "draft":
                try:
                    result = await run_in_threadpool(draft, self.store, data)
                except ValueError:
                    raise
                except Exception:
                    return JSONResponse(
                        {"error": "AI 服务不可用，未创建草稿；手动笔记仍可保存。"},
                        status_code=503,
                    )
            else:
                raise KeyError("接口不存在。")
            return JSONResponse(result)
        except Conflict as exc:
            return JSONResponse(
                {"error": str(exc), "code": "revision_conflict"}, status_code=409
            )
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except KeyError:
            return JSONResponse({"error": "记录不存在。"}, status_code=404)
        except asyncio.TimeoutError:
            return JSONResponse(
                {"error": "检索或模型请求超时；笔记未受影响。"}, status_code=504
            )

    async def file(self, request):
        try:
            item = self.store.artifact(request.path_params["ident"])
            path = self.store.artifact_path(item["id"])
            return FileResponse(
                path,
                media_type="application/pdf",
                filename=item["kind"] + ".pdf",
                content_disposition_type="attachment"
                if "download" in request.query_params
                else "inline",
            )
        except (KeyError, ValueError):
            return JSONResponse(
                {"error": "文件缺失或校验失败，不能保证定位。"}, status_code=404
            )

    async def page(self, request):
        return FileResponse(
            self.root / "research_assistant/web/static/reader/index.html"
        )

    def routes(self):
        return [
            Route("/reader", self.page),
            Route("/api/reading/artifacts/{ident}/file", self.file),
            Route(
                "/api/reading/{action}/{ident}", self.endpoint, methods=["GET", "POST"]
            ),
            Route("/api/reading/{action}", self.endpoint, methods=["GET", "POST"]),
            Mount(
                "/reader-assets",
                StaticFiles(
                    directory=self.root / ".reader_assets/node_modules/pdfjs-dist",
                    check_dir=False,
                ),
            ),
        ]
