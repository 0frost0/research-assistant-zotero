"""MinerU 高精度 PDF 解析、缓存读取和检索文档转换。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.documents import Document


from research_assistant.core.paths import PROJECT_ROOT
DEFAULT_CACHE_DIR = PROJECT_ROOT / ".cache" / "mineru"
DEFAULT_SERVER_URL = "http://127.0.0.1:30000"
CACHE_SCHEMA_VERSION = 1


class MinerUExtractionError(RuntimeError):
    """MinerU 未配置、远程服务不可用或输出不完整。"""


def _default_python() -> Path:
    configured = os.getenv("RESEARCH_ASSISTANT_MINERU_PYTHON")
    if configured:
        return Path(configured).expanduser().absolute()
    return PROJECT_ROOT / ".benchmark_envs" / "mineru" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _content_list_text(block: dict[str, Any]) -> str:
    """把 MinerU 的不同内容块转为可检索文本，同时保留表格和图注。"""
    parts: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
        elif isinstance(value, list):
            for item in value:
                add(item)

    for key in (
        "text",
        "list_items",
        "content",
        "table_body",
        "image_caption",
        "image_footnote",
        "table_caption",
        "table_footnote",
        "chart_caption",
        "chart_footnote",
        "code_body",
        "code_caption",
    ):
        add(block.get(key))
    return "\n".join(dict.fromkeys(parts))


class MinerUExtractor:
    """调用本地 MinerU 客户端，把远程 GPU 解析结果持久化到本机。"""

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        python_executable: Path | None = None,
        server_url: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self.cache_dir = (cache_dir or DEFAULT_CACHE_DIR).resolve()
        # Linux venv interpreters are symlinks. Resolving them loses pyvenv.cfg
        # discovery and runs system Python without the MinerU dependencies.
        self.python_executable = (python_executable or _default_python()).absolute()
        self.server_url = (
            server_url
            or os.getenv("RESEARCH_ASSISTANT_MINERU_URL")
            or DEFAULT_SERVER_URL
        ).rstrip("/")
        try:
            self.timeout_seconds = timeout_seconds or int(
                os.getenv("RESEARCH_ASSISTANT_MINERU_TIMEOUT", "1800")
            )
        except ValueError:
            self.timeout_seconds = 1800

    @property
    def configured(self) -> bool:
        return self.python_executable.is_file()

    def configuration(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "server_url": self.server_url,
            "python": str(self.python_executable),
        }

    def server_status(self, timeout_seconds: float = 3.0) -> tuple[bool, str | None]:
        """快速确认远程模型隧道可用，避免启动注定失败的长任务。"""
        try:
            with urllib.request.urlopen(
                f"{self.server_url}/health", timeout=timeout_seconds
            ) as response:
                if response.status == 200:
                    return True, None
                return False, f"健康检查返回 HTTP {response.status}"
        except (OSError, urllib.error.URLError) as exc:
            return False, str(exc)

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(f"mineru-cache-v{CACHE_SCHEMA_VERSION}".encode("ascii"))
        return digest.hexdigest()

    def cache_path(self, path: Path) -> Path:
        return self.cache_dir / self._file_digest(path.resolve())

    @staticmethod
    def _find_content_list(cache_path: Path) -> Path | None:
        matches = sorted(
            path
            for path in cache_path.rglob("*_content_list.json")
            if not path.name.endswith("_content_list_v2.json")
        )
        return matches[0] if matches else None

    def has_cache(self, path: Path) -> bool:
        try:
            cache_path = self.cache_path(path)
            return (cache_path / "manifest.json").is_file() and bool(
                self._find_content_list(cache_path)
            )
        except OSError:
            # 单个缓存目录损坏或不可读时，资料库仍可回退到快速 PDF 解析。
            return False

    def purge_cache(self, path: Path) -> None:
        """删除资料时同步清除同一内容的本地 MinerU 副本。"""
        cache_path = self.cache_path(path).resolve()
        if cache_path.parent == self.cache_dir and cache_path.is_dir():
            shutil.rmtree(cache_path)

    def _load_cache(
        self,
        path: Path,
        source: str,
        *,
        cache_hit: bool,
    ) -> tuple[list[Document], dict[str, Any]]:
        cache_path = self.cache_path(path)
        content_list_path = self._find_content_list(cache_path)
        if content_list_path is None:
            raise MinerUExtractionError("MinerU 缓存缺少 content_list.json")

        try:
            blocks = json.loads(content_list_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MinerUExtractionError(f"无法读取 MinerU 缓存：{exc}") from exc
        if not isinstance(blocks, list):
            raise MinerUExtractionError("MinerU content_list.json 格式不正确")

        documents: list[Document] = []
        block_counts: Counter[str] = Counter()
        pages: set[int] = set()
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            content_type = str(block.get("type") or "unknown")
            block_counts[content_type] += 1
            page_index = block.get("page_idx")
            page = page_index + 1 if isinstance(page_index, int) else None
            if page is not None:
                pages.add(page)
            text = _content_list_text(block)
            if not text:
                continue
            image_path = block.get("img_path")
            if image_path:
                candidate = Path(str(image_path))
                if not candidate.is_absolute():
                    candidate = content_list_path.parent / candidate
                image_path = str(candidate.resolve())
            documents.append(
                Document(
                    page_content=text,
                    metadata={
                        "source": source,
                        "file_type": ".pdf",
                        "page": page,
                        "extraction_method": "mineru",
                        "extraction_status": "ok",
                        "content_type": content_type,
                        "bbox": block.get("bbox"),
                        "image_path": image_path,
                        "block_index": block_index,
                    },
                )
            )

        manifest_path = cache_path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report = {
            "source": source,
            "parser": "mineru",
            "cache_hit": cache_hit,
            "total_pages": max(pages, default=0),
            "native_pages": 0,
            "ocr_pages": 0,
            "low_text_pages": 0,
            "empty_pages": 0,
            "blocks": len(blocks),
            "searchable_blocks": len(documents),
            "block_counts": dict(sorted(block_counts.items())),
            "parsed_at": manifest.get("parsed_at"),
            "pages": [],
        }
        return documents, report

    def load_cached(
        self,
        path: Path,
        *,
        source: str | None = None,
    ) -> tuple[list[Document], dict[str, Any]]:
        path = path.resolve()
        if not self.has_cache(path):
            raise MinerUExtractionError(f"没有可用的 MinerU 缓存：{path.name}")
        return self._load_cache(path, source or path.name, cache_hit=True)

    def parse(
        self,
        path: Path,
        *,
        source: str | None = None,
        force: bool = False,
    ) -> tuple[list[Document], dict[str, Any]]:
        path = path.resolve()
        source = source or path.name
        if path.suffix.lower() != ".pdf" or not path.is_file():
            raise MinerUExtractionError("MinerU 只能解析存在的 PDF 文件")
        if not self.configured:
            raise MinerUExtractionError(
                f"未找到 MinerU 客户端环境：{self.python_executable}"
            )
        if self.has_cache(path) and not force:
            return self._load_cache(path, source, cache_hit=True)
        healthy, reason = self.server_status()
        if not healthy:
            raise MinerUExtractionError(
                "MinerU 服务不可用。请先运行 scripts/start_mineru_tunnel.ps1；"
                f"健康检查详情：{reason}"
            )

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        destination = self.cache_path(path)
        temp_path = Path(
            tempfile.mkdtemp(prefix="mineru-", dir=str(self.cache_dir))
        )
        command = [
            str(self.python_executable),
            "-m",
            "mineru.cli.client",
            "--path",
            str(path),
            "--output",
            str(temp_path),
            "--backend",
            "vlm-http-client",
            "--url",
            self.server_url,
            "--client-side-output-generation",
            "true",
            "--image-analysis",
            "true",
        ]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                creationflags=flags,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                detail = detail[-1200:] if detail else "MinerU 客户端没有返回错误详情"
                raise MinerUExtractionError(f"MinerU 解析失败：{detail}")
            if self._find_content_list(temp_path) is None:
                raise MinerUExtractionError("MinerU 已结束，但没有生成 content_list.json")

            (temp_path / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": CACHE_SCHEMA_VERSION,
                        "source": source,
                        "source_sha256": self._file_digest(path),
                        "server_url": self.server_url,
                        "parsed_at": datetime.now().isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            if destination.exists():
                shutil.rmtree(destination)
            temp_path.replace(destination)
            return self._load_cache(path, source, cache_hit=False)
        except subprocess.TimeoutExpired as exc:
            raise MinerUExtractionError(
                f"MinerU 解析超过 {self.timeout_seconds} 秒，已停止等待"
            ) from exc
        finally:
            if temp_path.exists():
                shutil.rmtree(temp_path, ignore_errors=True)
