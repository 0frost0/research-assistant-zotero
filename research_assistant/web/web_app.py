"""科研助手本地网页服务。"""

import asyncio
import logging
import os
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from research_assistant.cli.app import save_plan
from research_assistant.retrieval.knowledge_base import (
    DEFAULT_THRESHOLDS,
    SUPPORTED_BACKENDS,
    SUPPORTED_SUFFIXES,
    LiteratureIndex,
)
from research_assistant.core.models import ResearchContext
from research_assistant.ingestion.mineru_extractor import MinerUExtractor
from research_assistant.agents.research_agent import build_agent, run_agent


from research_assistant.core.paths import PROJECT_ROOT, STATIC_DIR
LIBRARY_DIR = PROJECT_ROOT / "library"
# 当前运行环境可能没有权限在 D:\\workspace 下新建目录；诊断日志放到本机临时目录。
LOG_DIR = Path(tempfile.gettempdir()) / "research_assistant_logs"
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# 网页服务也要主动读取 .env；不能依赖启动命令所在的当前目录。
for env_path in (
    PROJECT_ROOT / ".env",
    PROJECT_ROOT.parent / ".env",
    PROJECT_ROOT.parent / "langchain" / ".env",
):
    if env_path.exists():
        load_dotenv(env_path)
        break

# LangSmith 上报是可选诊断能力；默认关闭，避免网络受限时干扰主流程。
if os.getenv("RESEARCH_ASSISTANT_ENABLE_LANGSMITH", "false").lower() not in {
    "1",
    "true",
    "yes",
}:
    os.environ["LANGSMITH_TRACING"] = "false"

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_DIR / "web_app.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
logger = logging.getLogger(__name__)

try:
    REQUEST_TIMEOUT_SECONDS = float(
        os.getenv("RESEARCH_ASSISTANT_REQUEST_TIMEOUT", "90")
    )
except ValueError:
    REQUEST_TIMEOUT_SECONDS = 90.0

_cached_signature: tuple | None = None
_cached_backend: str | None = None
_cached_index: LiteratureIndex | None = None
_index_lock = threading.Lock()
_mineru_jobs: dict[str, dict[str, Any]] = {}
_background_tasks: set[asyncio.Task] = set()
_mineru_start_lock = threading.Lock()


class RequestValidationError(ValueError):
    """用户输入不符合 API 约束，应返回 400 而不是服务器 500。"""


def library_signature() -> tuple:
    """文件变化后自动使内存索引失效。"""
    return tuple(
        (str(path.relative_to(LIBRARY_DIR)), path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(LIBRARY_DIR.rglob("*"))
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def get_index(backend: str) -> LiteratureIndex:
    global _cached_signature, _cached_backend, _cached_index

    signature = library_signature()
    with _index_lock:
        if (
            _cached_index is None
            or signature != _cached_signature
            or backend != _cached_backend
        ):
            _cached_index = LiteratureIndex(LIBRARY_DIR, backend=backend)
            _cached_signature = signature
            _cached_backend = backend
    return _cached_index


def reset_index_cache() -> None:
    """上传、删除或高精度解析完成后，使下一次请求重建索引。"""
    global _cached_signature, _cached_backend, _cached_index

    with _index_lock:
        _cached_signature = None
        _cached_backend = None
        _cached_index = None


def safe_library_path(file_path: str) -> Path:
    """阻止 ../ 等路径穿越，只允许操作 library 内的文件。"""
    candidate = (LIBRARY_DIR / file_path).resolve()
    if not candidate.is_relative_to(LIBRARY_DIR.resolve()):
        raise ValueError("非法文件路径")
    return candidate


def parse_search_options(data: dict[str, Any]) -> dict[str, Any]:
    backend = str(data.get("backend", "multimodal"))
    if backend not in SUPPORTED_BACKENDS:
        raise RequestValidationError(f"不支持的检索方式：{backend}")

    sources_value = data.get("sources")
    sources = None
    if sources_value is not None:
        if not isinstance(sources_value, list) or not all(
            isinstance(item, str) for item in sources_value
        ):
            raise RequestValidationError("sources 必须是文件名列表")
        if not sources_value:
            raise RequestValidationError("请至少选择一份资料")
        available = {item[0] for item in library_signature()}
        unknown = sorted(set(sources_value) - available)
        if unknown:
            raise RequestValidationError(f"资料不存在：{', '.join(unknown)}")
        sources = list(dict.fromkeys(sources_value))

    file_type = str(data.get("file_type", "all")).lower()
    if file_type not in {"all", "pdf", "md", "txt"}:
        raise RequestValidationError(f"不支持的文件类型：{file_type}")
    file_types = None if file_type == "all" else [file_type]

    try:
        min_score = float(
            data.get("min_score", DEFAULT_THRESHOLDS[backend])
        )
        k = int(data.get("k", 5))
    except (TypeError, ValueError) as exc:
        raise RequestValidationError("相关度和结果数量必须是数字") from exc
    if not 0 <= min_score <= 1:
        raise RequestValidationError("最低相关度必须在 0 到 1 之间")
    if not 1 <= k <= 20:
        raise RequestValidationError("结果数量必须在 1 到 20 之间")

    return {
        "backend": backend,
        "sources": sources,
        "file_types": file_types,
        "min_score": min_score,
        "k": k,
    }


async def homepage(_: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def library_api(request: Request) -> JSONResponse:
    if request.method == "GET":
        mineru = MinerUExtractor()
        files = [
            {
                "name": str(path.relative_to(LIBRARY_DIR)),
                "size": path.stat().st_size,
                "type": path.suffix.lower().removeprefix(".").upper(),
                "mineru_cached": (
                    mineru.has_cache(path) if path.suffix.lower() == ".pdf" else False
                ),
            }
            for path in sorted(LIBRARY_DIR.rglob("*"))
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        ]
        return JSONResponse({"files": files})

    form = await request.form()
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", None):
        return JSONResponse({"error": "没有选择文件"}, status_code=400)

    filename = Path(upload.filename).name
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        return JSONResponse(
            {"error": "只支持 PDF、Markdown 和 TXT"},
            status_code=400,
        )

    content = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "文件不能超过 25 MB"}, status_code=400)

    destination = safe_library_path(filename)
    if destination.exists():
        return JSONResponse({"error": "同名文件已经存在"}, status_code=409)
    destination.write_bytes(content)
    reset_index_cache()
    reading = {}
    if suffix == ".pdf":
        try:
            reading = await reading_api.uploaded(destination, filename)
        except Exception:
            reading = {"translation_warning": "文件已上传，但阅读器暂不能处理该 PDF；请检查文件是否加密或损坏。"}
    return JSONResponse({"ok": True, "name": filename, **reading}, status_code=201)


async def delete_library_file(request: Request) -> JSONResponse:
    try:
        path = safe_library_path(request.path_params["file_path"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    if path.suffix.lower() == ".pdf":
        # Preserve a hash-verified original before removing it from the retrieval library.
        # If archival fails, deletion stops instead of orphaning notes.
        try:
            await run_in_threadpool(reading_api.store.register, path, str(path.relative_to(LIBRARY_DIR)))
        except Exception:
            return JSONResponse({"error": "原件归档失败，已停止删除；请检查 PDF 和备份。"}, status_code=409)
        MinerUExtractor().purge_cache(path)
    path.unlink()
    reset_index_cache()
    return JSONResponse({"ok": True})


async def _run_mineru_job(
    job_id: str,
    path: Path,
    source: str,
    force: bool,
) -> None:
    job = _mineru_jobs[job_id]
    job["status"] = "running"
    try:
        _, report = await run_in_threadpool(
            MinerUExtractor().parse,
            path,
            source=source,
            force=force,
        )
        reset_index_cache()
        job.update({"status": "completed", "report": report})
    except Exception as exc:
        logger.exception(
            "MinerU 解析失败：job_id=%s source=%r", job_id, source
        )
        job.update(
            {
                "status": "failed",
                "error": f"{type(exc).__name__}: {str(exc).strip()}",
            }
        )
    finally:
        job["finished_at"] = datetime.now().isoformat(timespec="seconds")


async def mineru_parse_api(request: Request) -> JSONResponse:
    """启动单份 PDF 的后台高精度解析，避免长请求阻塞网页。"""
    try:
        data = await request.json()
        source = str(data.get("source", "")).strip()
        path = safe_library_path(source)
    except Exception:
        return JSONResponse({"error": "PDF 文件参数不正确"}, status_code=400)
    if not path.is_file() or path.suffix.lower() != ".pdf":
        return JSONResponse({"error": "只能高精度解析资料库中的 PDF"}, status_code=400)

    mineru = MinerUExtractor()
    if not mineru.configured:
        return JSONResponse(
            {
                "error": "本机尚未配置 MinerU 客户端环境",
                "error_code": "mineru_not_configured",
            },
            status_code=503,
        )
    healthy, detail = await run_in_threadpool(mineru.server_status)
    if not healthy:
        return JSONResponse(
            {
                "error": "MinerU 服务不可用，请先运行 scripts/start_mineru_tunnel.ps1。",
                "error_code": "mineru_unavailable",
                "error_detail": detail,
            },
            status_code=503,
        )

    with _mineru_start_lock:
        active = next(
            (
                (existing_id, job)
                for existing_id, job in _mineru_jobs.items()
                if job["status"] in {"queued", "running"}
            ),
            None,
        )
        if active is not None:
            existing_id, job = active
            if job["source"] == source:
                return JSONResponse({"job_id": existing_id, **job}, status_code=202)
            return JSONResponse(
                {
                    "error": f"正在精析 {job['source']}，请等待该任务完成。",
                    "error_code": "mineru_busy",
                    "job_id": existing_id,
                },
                status_code=409,
            )

        job_id = uuid.uuid4().hex[:12]
        _mineru_jobs[job_id] = {
            "source": source,
            "status": "queued",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        task = asyncio.create_task(
            _run_mineru_job(job_id, path, source, bool(data.get("force")))
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return JSONResponse({"job_id": job_id, **_mineru_jobs[job_id]}, status_code=202)


async def mineru_job_api(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    job = _mineru_jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "解析任务不存在"}, status_code=404)
    return JSONResponse({"job_id": job_id, **job})


def execute_research_request(data: dict[str, Any]) -> dict[str, Any]:
    mode = data.get("mode", "answer")
    if mode not in {"answer", "plan"}:
        raise RequestValidationError(f"不支持的工作模式：{mode}")
    question = str(data.get("question", "")).strip()
    if mode == "plan":
        question = "请基于文献证据制定结构化研究计划。研究目标：" + question

    try:
        weekly_hours = float(data.get("hours", 10))
    except (TypeError, ValueError) as exc:
        raise RequestValidationError("每周时间必须是数字") from exc
    if not 1 <= weekly_hours <= 80:
        raise RequestValidationError("每周时间必须在 1 到 80 小时之间")

    options = parse_search_options(data)
    index = get_index(options["backend"])
    agent = build_agent(
        index,
        sources=options["sources"],
        file_types=options["file_types"],
        min_score=options["min_score"],
    )
    response = run_agent(
        agent,
        question,
        ResearchContext(
            research_topic=str(data.get("topic", "当前科研项目")),
            weekly_hours=weekly_hours,
            mode=mode,
        ),
    )

    saved_path = None
    if bool(data.get("save_plan")) and response.mode == "plan":
        saved_path = str(save_plan(response))
    return {
        "response": response.model_dump(),
        "saved_path": saved_path,
        "searches": _public_searches(agent.search_history),
    }


def _public_result(result: dict[str, Any]) -> dict[str, Any]:
    """Replace private local image paths with a controlled HTTP endpoint."""
    public = dict(result)
    public.pop("image_path", None)
    if public.get("modality") in {"image", "multimodal"} and public.get(
        "evidence_id"
    ):
        public["image_url"] = f"/api/evidence-image/{public['evidence_id']}"
    else:
        public["image_url"] = None
    return public


def _public_searches(searches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **search,
            "results": [_public_result(item) for item in search.get("results", [])],
        }
        for search in searches
    ]


def execute_search_request(data: dict[str, Any]) -> dict[str, Any]:
    query = str(data.get("question", "")).strip()
    if not query:
        raise RequestValidationError("请输入需要检索的问题")
    options = parse_search_options(data)
    index = get_index(options["backend"])
    results = index.search_results(
        query,
        k=options["k"],
        min_score=options["min_score"],
        sources=options["sources"],
        file_types=options["file_types"],
    )
    if results:
        effective_backend = results[0].backend
    elif options["backend"] == "qdrant" and index.persistent_index is None:
        effective_backend = "qdrant-fallback-bge"
    elif options["backend"] == "multimodal" and index.unified_index is None:
        effective_backend = "multimodal-fallback-tfidf"
    else:
        effective_backend = options["backend"]
    return {
        "query": query,
        "backend": options["backend"],
        "effective_backend": effective_backend,
        "warning": index.unified_error or index.persistent_error,
        "min_score": options["min_score"],
        "results": [_public_result(result.model_dump()) for result in results],
    }


async def evidence_image_api(request: Request):
    from research_assistant.storage.evidence_store import EvidenceStore

    visual_id = request.path_params["visual_id"]
    store = EvidenceStore(PROJECT_ROOT / ".data" / "evidence.sqlite3")
    image_path = await run_in_threadpool(store.visual_path, visual_id)
    cache_root = (PROJECT_ROOT / ".cache" / "mineru").resolve()
    if (
        image_path is None
        or not image_path.is_file()
        or not image_path.is_relative_to(cache_root)
    ):
        return JSONResponse({"error": "图片证据不存在"}, status_code=404)
    return FileResponse(
        image_path,
        headers={"Cache-Control": "private, max-age=3600"},
    )


def execute_pdf_report_request() -> dict[str, Any]:
    """按需建立本地索引并返回 PDF 逐页提取报告。"""
    index = get_index("tfidf")
    return {
        "reports": list(index.pdf_reports.values()),
        "ocr_enabled": os.getenv(
            "RESEARCH_ASSISTANT_ENABLE_OCR", "true"
        ).lower() in {"1", "true", "yes"},
        "ocr_language": os.getenv("RESEARCH_ASSISTANT_OCR_LANG", "eng"),
    }


async def search_api(request: Request) -> JSONResponse:
    try:
        data = await request.json()
        result = await run_in_threadpool(execute_search_request, data)
        return JSONResponse(result)
    except RequestValidationError as exc:
        return JSONResponse(
            {"error": str(exc), "error_code": "invalid_input"},
            status_code=400,
        )
    except Exception as exc:
        request_id = uuid.uuid4().hex[:12]
        logger.exception("本地检索失败：request_id=%s", request_id)
        return JSONResponse(
            {
                "error": f"{type(exc).__name__}: {str(exc).strip()}",
                "error_code": "search_failed",
                "request_id": request_id,
            },
            status_code=500,
        )


async def pdf_reports_api(_: Request) -> JSONResponse:
    try:
        result = await run_in_threadpool(execute_pdf_report_request)
        return JSONResponse(result)
    except Exception as exc:
        request_id = uuid.uuid4().hex[:12]
        logger.exception("PDF 提取检查失败：request_id=%s", request_id)
        return JSONResponse(
            {
                "error": f"PDF 提取检查失败：{type(exc).__name__}: {exc}",
                "error_code": "pdf_extraction_failed",
                "request_id": request_id,
            },
            status_code=500,
        )
async def health_api(_: Request) -> JSONResponse:
    """只检查本地 HTTP 服务存活；外部依赖的详细状态仍由 /api/status 提供。"""
    return JSONResponse({"ok": True, "service": "research_assistant"})


async def status_api(_: Request) -> JSONResponse:
    files = library_signature()
    mineru = MinerUExtractor().configuration()
    from research_assistant.retrieval.embeddings.remote_embeddings import embedding_service_status

    embedding = await run_in_threadpool(embedding_service_status)
    try:
        from research_assistant.retrieval.qdrant_index import qdrant_service_status

        qdrant = await run_in_threadpool(
            qdrant_service_status,
            PROJECT_ROOT / ".data" / "evidence.sqlite3",
        )
    except Exception as exc:
        qdrant = {
            "available": False,
            "detail": f"{type(exc).__name__}: {exc}",
        }
    return JSONResponse(
        {
            "ok": True,
            "files": len(files),
            "model": os.getenv("OPENAI_MODEL", "未配置"),
            "model_configured": bool(
                os.getenv("OPENAI_MODEL")
                and os.getenv("OPENAI_API_KEY")
                and os.getenv("OPENAI_BASE_URL")
            ),
            "langsmith_enabled": os.getenv("LANGSMITH_TRACING", "false").lower()
            in {"1", "true", "yes"},
            "default_thresholds": DEFAULT_THRESHOLDS,
            "mineru": mineru,
            "qdrant": qdrant,
            "embedding": embedding,
            "active_unified_collection": (
                _cached_index.unified_index.collection_name
                if _cached_index is not None and _cached_index.unified_index is not None
                else None
            ),
        }
    )


async def run_api(request: Request) -> JSONResponse:
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "请求 JSON 格式不正确"}, status_code=400)
    if data.get("allow_external") is not True:
        return JSONResponse(
            {
                "error": "请先确认允许将命中的文献片段发送给 DeepSeek",
                "error_code": "external_permission_required",
            },
            status_code=403,
        )
    if not str(data.get("question", "")).strip():
        return JSONResponse({"error": "请输入问题或研究目标"}, status_code=400)

    try:
        data["_request_id"] = uuid.uuid4().hex[:12]
        # 即使后台模型异常变慢，浏览器也不能无限停留在“检索与分析”。
        result = await asyncio.wait_for(
            run_in_threadpool(execute_research_request, data),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        return JSONResponse(result)
    except asyncio.TimeoutError:
        request_id = data.get("_request_id", uuid.uuid4().hex[:12])
        logger.error(
            "研究请求超时：request_id=%s timeout=%.1fs",
            request_id,
            REQUEST_TIMEOUT_SECONDS,
        )
        return JSONResponse(
            {
                "error": "分析超过时间上限，已停止等待。请缩小问题范围后重试。",
                "error_code": "agent_timeout",
                "request_id": request_id,
                "time": datetime.now().isoformat(timespec="seconds"),
            },
            status_code=504,
        )
    except RequestValidationError as exc:
        return JSONResponse(
            {"error": str(exc), "error_code": "invalid_input"},
            status_code=400,
        )
    except Exception as exc:
        # 把完整堆栈留在本机日志中，网页只显示可读的异常类型和原因。
        logger.exception(
            "研究请求失败：request_id=%s backend=%r mode=%r question_length=%d",
            data["_request_id"],
            data.get("backend"),
            data.get("mode"),
            len(str(data.get("question", ""))),
        )
        detail = str(exc).strip() or "没有提供详细错误信息"
        error_text = str(exc).strip()
        error_code = "agent_failed"
        user_message = f"{type(exc).__name__}: {error_text}"
        if "connection error" in error_text.lower() or "winerror 10013" in error_text.lower():
            error_code = "external_model_connection"
            user_message = (
                "无法连接 DeepSeek。请检查本机防火墙/代理，或确认服务是以允许网络访问的方式启动。"
            )
        elif "401" in error_text or "authentication" in error_text.lower():
            error_code = "external_model_auth"
            user_message = "DeepSeek 鉴权失败，请检查 .env 中的 API Key 是否有效。"
        elif "429" in error_text or "rate limit" in error_text.lower():
            error_code = "external_model_rate_limit"
            user_message = "DeepSeek 请求过于频繁或额度受限，请稍后再试。"
        elif "402" in error_text or "insufficient balance" in error_text.lower():
            error_code = "external_model_balance"
            user_message = "DeepSeek API 余额不足，请充值或更换可用的 API Key 后重试。"
        return JSONResponse(
            {
                "error": user_message,
                "error_code": error_code,
                "error_detail": detail,
                "request_id": data["_request_id"],
                "log_file": str((LOG_DIR / "web_app.log").resolve()),
                "time": datetime.now().isoformat(timespec="seconds"),
            },
            status_code=500,
        )


routes = [
    Route("/", homepage),
    Route("/api/library", library_api, methods=["GET", "POST"]),
    Route("/api/search", search_api, methods=["POST"]),
    Route("/api/evidence-image/{visual_id}", evidence_image_api, methods=["GET"]),
    Route("/api/pdf-reports", pdf_reports_api, methods=["GET"]),
    Route("/api/pdf-parse", mineru_parse_api, methods=["POST"]),
    Route("/api/pdf-parse/{job_id}", mineru_job_api, methods=["GET"]),
    Route("/api/health", health_api, methods=["GET"]),
    Route("/api/status", status_api, methods=["GET"]),
    Route(
        "/api/library/{file_path:path}",
        delete_library_file,
        methods=["DELETE"],
    ),
    Route("/api/run", run_api, methods=["POST"]),
    Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
]

from research_assistant.web.lab_api import make_lab_routes

routes.extend(make_lab_routes(PROJECT_ROOT, get_index, safe_library_path))
from research_assistant.web.project_api import make_project_routes

routes.extend(make_project_routes(PROJECT_ROOT, get_index))
from research_assistant.web.comparison_api import make_comparison_routes

routes.extend(make_comparison_routes(PROJECT_ROOT, get_index, parse_search_options))
from research_assistant.web.reading_api import ReadingAPI

reading_api = ReadingAPI(PROJECT_ROOT, get_index)
routes.extend(reading_api.routes())
from research_assistant.web.knowledge_api import KnowledgeAPI

knowledge_api = KnowledgeAPI(PROJECT_ROOT)
routes.extend(knowledge_api.routes())
app = Starlette(debug=False, routes=routes, lifespan=reading_api.lifespan)


if __name__ == "__main__":
    # 只监听本机，局域网和公网设备无法直接访问。
    uvicorn.run(app, host="127.0.0.1", port=8765)
