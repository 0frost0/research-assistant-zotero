"""PDF 逐页提取：优先原生文本，必要时才使用 OCR。"""

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import pypdf
from langchain_core.documents import Document


MIN_NATIVE_TEXT_CHARS = 80
OCR_DPI = 200
OCR_TIMEOUT_SECONDS = 60


class PDFExtractionError(RuntimeError):
    """PDF 无法打开或逐页处理时抛出的可读异常。"""


@dataclass(frozen=True)
class PDFPageReport:
    page: int
    method: Literal["native", "ocr", "none"]
    status: Literal["ok", "low_text", "empty", "ocr_unavailable", "ocr_failed"]
    characters: int
    image_count: int
    warning: str | None = None


@dataclass(frozen=True)
class PDFExtractionReport:
    source: str
    total_pages: int
    native_pages: int
    ocr_pages: int
    low_text_pages: int
    empty_pages: int
    pages: list[PDFPageReport]
    parser: str = "pypdf+tesseract"
    cache_hit: bool = False
    parser: str = "pypdf+tesseract"
    cache_hit: bool = False

    def model_dump(self) -> dict:
        return asdict(self)


def normalize_pdf_text(text: str) -> str:
    """清理控制字符和断词，同时保留段落边界。"""
    text = text.replace("\x00", "").replace("\u00ad", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[a-z])", "", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _page_image_count(page: pypdf.PageObject) -> int:
    """只统计页面顶层图片对象，不解码图片数据。"""
    try:
        resources = page.get("/Resources") or {}
        xobjects = resources.get("/XObject") or {}
        return sum(
            1
            for reference in xobjects.values()
            if reference.get_object().get("/Subtype") == "/Image"
        )
    except Exception:
        return 0


class PDFExtractor:
    def __init__(
        self,
        *,
        enable_ocr: bool | None = None,
        ocr_language: str | None = None,
        min_native_chars: int = MIN_NATIVE_TEXT_CHARS,
    ) -> None:
        if enable_ocr is None:
            enable_ocr = os.getenv(
                "RESEARCH_ASSISTANT_ENABLE_OCR", "true"
            ).lower() in {"1", "true", "yes"}
        self.enable_ocr = enable_ocr
        self.ocr_language = ocr_language or os.getenv(
            "RESEARCH_ASSISTANT_OCR_LANG", "eng"
        )
        self.min_native_chars = min_native_chars
        self.pdftoppm = shutil.which("pdftoppm")
        self.tesseract = shutil.which("tesseract")
        self._available_ocr_languages: set[str] | None = None

    def _ocr_available(self) -> tuple[bool, str | None]:
        if not self.enable_ocr:
            return False, "OCR 已关闭"
        if not self.pdftoppm or not self.tesseract:
            return False, "缺少 pdftoppm 或 tesseract"
        if self._available_ocr_languages is None:
            try:
                completed = subprocess.run(
                    [self.tesseract, "--list-langs"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    check=True,
                )
                self._available_ocr_languages = {
                    line.strip()
                    for line in completed.stdout.splitlines()
                    if line.strip() and not line.lower().startswith("list of")
                }
            except (OSError, subprocess.SubprocessError):
                return False, "无法读取 Tesseract 语言列表"
        requested = set(self.ocr_language.split("+"))
        missing = requested - self._available_ocr_languages
        if missing:
            return False, f"缺少 OCR 语言包：{', '.join(sorted(missing))}"
        return True, None

    def _ocr_page(self, path: Path, page_number: int) -> str:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with tempfile.TemporaryDirectory(prefix="research_pdf_ocr_") as temp_dir:
            image_prefix = Path(temp_dir) / "page"
            subprocess.run(
                [
                    self.pdftoppm,
                    "-f",
                    str(page_number),
                    "-l",
                    str(page_number),
                    "-singlefile",
                    "-png",
                    "-r",
                    str(OCR_DPI),
                    str(path),
                    str(image_prefix),
                ],
                capture_output=True,
                timeout=OCR_TIMEOUT_SECONDS,
                check=True,
                creationflags=flags,
            )
            completed = subprocess.run(
                [
                    self.tesseract,
                    str(image_prefix.with_suffix(".png")),
                    "stdout",
                    "-l",
                    self.ocr_language,
                    "--psm",
                    "6",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=OCR_TIMEOUT_SECONDS,
                check=True,
                creationflags=flags,
            )
            return normalize_pdf_text(completed.stdout)

    def extract(
        self,
        path: Path,
        *,
        source: str | None = None,
    ) -> tuple[list[Document], PDFExtractionReport]:
        path = path.resolve()
        source = source or path.name
        try:
            reader = pypdf.PdfReader(path)
        except Exception as exc:
            raise PDFExtractionError(f"无法打开 PDF：{source}：{exc}") from exc

        documents: list[Document] = []
        page_reports: list[PDFPageReport] = []
        native_pages = 0
        ocr_pages = 0
        low_text_pages = 0
        empty_pages = 0

        for page_number, page in enumerate(reader.pages, start=1):
            warning = None
            image_count = _page_image_count(page)
            try:
                text = normalize_pdf_text(page.extract_text() or "")
            except Exception as exc:
                text = ""
                warning = f"原生文本提取失败：{type(exc).__name__}"

            method: Literal["native", "ocr", "none"] = "native" if text else "none"
            status: Literal[
                "ok", "low_text", "empty", "ocr_unavailable", "ocr_failed"
            ] = "ok"
            if len(text) < self.min_native_chars:
                available, reason = self._ocr_available()
                if available:
                    try:
                        ocr_text = self._ocr_page(path, page_number)
                        if len(ocr_text) > len(text):
                            text = ocr_text
                            method = "ocr"
                            ocr_pages += 1
                    except (OSError, subprocess.SubprocessError) as exc:
                        status = "ocr_failed"
                        warning = f"OCR 失败：{type(exc).__name__}"
                else:
                    status = "ocr_unavailable"
                    warning = reason

            if len(text) >= self.min_native_chars:
                status = "ok"
                if method == "native":
                    native_pages += 1
            elif text:
                status = "low_text" if warning is None else status
                low_text_pages += 1
            else:
                status = "empty" if warning is None else status
                empty_pages += 1

            if text:
                documents.append(
                    Document(
                        page_content=text,
                        metadata={
                            "source": source,
                            "file_type": ".pdf",
                            "page": page_number,
                            "extraction_method": method,
                            "extraction_status": status,
                            "image_count": image_count,
                        },
                    )
                )
            page_reports.append(
                PDFPageReport(
                    page=page_number,
                    method=method,
                    status=status,
                    characters=len(text),
                    image_count=image_count,
                    warning=warning,
                )
            )

        return documents, PDFExtractionReport(
            source=source,
            total_pages=len(reader.pages),
            native_pages=native_pages,
            ocr_pages=ocr_pages,
            low_text_pages=low_text_pages,
            empty_pages=empty_pages,
            pages=page_reports,
        )
