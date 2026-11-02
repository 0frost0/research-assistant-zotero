"""Durable, sequential MinerU jobs. Original PDFs and fast text remain available."""
import asyncio
import hashlib
import json
from pathlib import Path
import uuid

from research_assistant.knowledge.store import now, encoded
from research_assistant.ingestion.mineru_extractor import MinerUExtractor


class ParsingJobs:
    def __init__(self, store):
        self.store = store
        self.tasks = set()
        self.gate = asyncio.Lock()
        with store.transaction() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS parsing_jobs(
                id TEXT PRIMARY KEY, sha TEXT NOT NULL, profile TEXT NOT NULL,
                status TEXT NOT NULL, error TEXT, updated_at TEXT NOT NULL)""")
            con.execute("UPDATE parsing_jobs SET status='interrupted', error=?, updated_at=? WHERE status IN ('queued','running')",
                        ("应用重启中断了任务；可手动重试。", now()))

    def list(self):
        with self.store.transaction() as con:
            return [dict(row) for row in con.execute(
                "SELECT id,sha,status,error,updated_at FROM parsing_jobs ORDER BY updated_at DESC LIMIT 100")]

    def submit(self, sha, config):
        if not self.store.object_exists(sha):
            raise ValueError("原文不存在")
        if config.get("mode") not in {"local", "remote"}:
            raise ValueError("请先配置 MinerU")
        profile = hashlib.sha256(encoded(config).encode()).hexdigest()
        with self.store.transaction() as con:
            original = con.execute("SELECT report FROM objects WHERE hash=?", (sha,)).fetchone()
            parsed = json.loads(original[0]).get("mineru", {})
            if parsed.get("profile") == profile and parsed.get("blocks") and all(
                    not block["metadata"].get("image_path") or Path(block["metadata"]["image_path"]).is_file()
                    for block in parsed["blocks"]):
                return {"id": None, "status": "succeeded", "cached": True}
            row = con.execute("SELECT id,status FROM parsing_jobs WHERE sha=? AND profile=? AND status IN ('queued','running')",
                              (sha, profile)).fetchone()
            if row:
                return dict(row)
            ident = uuid.uuid4().hex
            con.execute("INSERT INTO parsing_jobs VALUES(?,?,?,?,?,?)",
                        (ident, sha, profile, "queued", None, now()))
        task = asyncio.create_task(self.run(ident, sha, dict(config), profile))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return {"id": ident, "status": "queued"}

    def state(self, ident, status, error=None):
        with self.store.transaction() as con:
            con.execute("UPDATE parsing_jobs SET status=?,error=?,updated_at=? WHERE id=?",
                        (status, error, now(), ident))

    async def run(self, ident, sha, config, profile):
        async with self.gate:
            self.state(ident, "running")
            try:
                await asyncio.to_thread(self.parse, sha, config, profile)
            except asyncio.CancelledError:
                self.state(ident, "interrupted", "应用停止；请重试。")
                raise
            except Exception as exc:
                # Do not expose CLI stderr (may contain document text or credentials).
                self.state(ident, "failed", f"{type(exc).__name__}：解析失败，检查 MinerU 环境、GPU、模型文件、地址和超时；原文保留。")
            else:
                self.state(ident, "succeeded")

    def parse(self, sha, config, profile):
        parser = MinerUExtractor(cache_dir=self.store.home / "mineru" / profile,
                                python_executable=Path(config["python"]) if config.get("python") else None,
                                server_url=config.get("url") or None,
                                timeout_seconds=config.get("timeout", 1800), mode=config["mode"])
        documents, report = parser.parse(self.store.objects / (sha + ".pdf"), source=sha + ".pdf")
        if not documents:
            raise ValueError("解析未生成可用内容")
        # Keep fast page text immutable for existing note quote verification.
        # Blocks are a separate derived representation with image/bbox metadata.
        blocks = [{"text": doc.page_content, "metadata": doc.metadata} for doc in documents]
        with self.store.transaction() as con:
            row = con.execute("SELECT report FROM objects WHERE hash=?", (sha,)).fetchone()
            if row is None:
                raise ValueError("原文记录已不存在")
            original = json.loads(row[0])
            original["mineru"] = {"report": report, "blocks": blocks, "profile": profile}
            con.execute("UPDATE objects SET report=? WHERE hash=?", (encoded(original), sha))
