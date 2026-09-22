"""Worker exit races use temporary data and mocked processes, never providers."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from research_assistant.memory.store import ReadingStore
from research_assistant.translation.service import TranslationService
from tests.test_reading_memory import pdf


class TranslationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "library").mkdir()
        source = root / "library" / "synthetic.pdf"
        pdf(source)
        self.store = ReadingStore(root)
        self.paper = self.store.register(source, source.name)
        self.service = TranslationService(self.store)

    def job(self, version=1):
        job, _ = self.store.reserve_job(self.paper["id"], {"fixture": version})
        self.service.ledger.account(job["id"])
        return job

    async def test_cleanup_tolerates_process_exiting_before_killpg(self):
        process = SimpleNamespace(pid=987654, returncode=None, wait=AsyncMock(return_value=0))
        self.service.process = process
        with patch("research_assistant.translation.service.os.name", "posix"), patch(
            "research_assistant.translation.service.os.killpg",
            create=True, side_effect=ProcessLookupError,
        ), patch("signal.SIGKILL", 9, create=True):
            await self.service.kill_process()
        process.wait.assert_awaited_once()

    async def test_exited_worker_does_not_leave_blocked_job_running(self):
        first, second = self.job(), self.job(2)
        process = SimpleNamespace(pid=987654, returncode=None, wait=AsyncMock(return_value=0))
        seen = []

        async def fail(job):
            seen.append(job["id"])
            self.service.ledger.start(job["id"], job["attempt"])
            if job["id"] == first["id"]:
                self.service.process = process
                request = self.service.ledger.reserve(job["id"], job["attempt"], {})
                self.service.ledger.finish(
                    request, usage={"total_tokens": 2048},
                    error="incomplete_response", halt=True,
                )
                self.store.update_job(job["id"], stage="Save PDF", progress=99)
                raise ValueError("incomplete response")
            raise ValueError("synthetic second failure")

        with patch.object(self.service, "execute", side_effect=fail), patch(
            "research_assistant.translation.service.os.name", "posix"
        ), patch("research_assistant.translation.service.os.killpg",
                 create=True, side_effect=ProcessLookupError), patch("signal.SIGKILL", 9, create=True):
            task = asyncio.create_task(self.service.run())
            try:
                for _ in range(100):
                    await asyncio.sleep(0.005)
                    if task.done():
                        await task
                    if self.store.job(second["id"])["state"] == "failed":
                        break
                result = self.store.job(first["id"])
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["error_code"], "incomplete_response")
                self.assertIsNone(result["progress"])
                self.assertIsNone(result["artifact_id"])
                self.assertEqual(seen, [first["id"], second["id"]])
                account = self.service.ledger.summary(first["id"])
                self.assertEqual(account["reported_tokens"], 2048)
                self.assertEqual(account["attempts"][0]["state"], "failed")
            finally:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, ProcessLookupError):
                    pass

    async def test_cleanup_error_still_records_failure_before_stopping_queue(self):
        job = self.job()
        with patch.object(self.service, "execute", new=AsyncMock(side_effect=ValueError("synthetic"))), patch.object(
            self.service, "kill_process", new=AsyncMock(side_effect=PermissionError("private diagnostic"))
        ):
            with self.assertRaises(PermissionError):
                await self.service.run()
        result = self.store.job(job["id"])
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(result["progress"])
        self.assertNotIn("private diagnostic", str(result))

    async def test_restart_preserves_recorded_provider_failure_without_retry(self):
        job = self.job()
        self.store.update_job(job["id"], state="running", stage="Save PDF", progress=99)
        self.service.ledger.start(job["id"], 1)
        request = self.service.ledger.reserve(job["id"], 1, {})
        self.service.ledger.finish(request, usage={"total_tokens": 2048},
                                   error="incomplete_response", halt=True)
        execute = AsyncMock()
        with patch.object(self.service, "execute", execute):
            await self.service.start()
            await asyncio.sleep(0)
            await self.service.stop()
        result = self.store.job(job["id"])
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error_code"], "incomplete_response")
        self.assertIn("截断", result["error"])
        self.assertIsNone(result["progress"])
        self.assertEqual(self.service.ledger.summary(job["id"])["requests"], 1)
        execute.assert_not_called()

    async def test_blocked_worker_stops_even_without_stdout(self):
        job = self.job()
        self.service.ledger.start(job["id"], 1)
        request = self.service.ledger.reserve(job["id"], 1, {})
        self.service.ledger.finish(request, error="output_truncated", halt=True)
        stream = asyncio.StreamReader()
        self.service.process = SimpleNamespace(stdout=stream)
        lines = self.service.worker_lines(job["id"])
        with self.assertRaisesRegex(ValueError, "length"):
            await asyncio.wait_for(anext(lines), timeout=2.5)
        self.assertIsNone(stream._waiter)
        self.assertEqual(self.service.ledger.summary(job["id"])["requests"], 1)

    async def test_blocked_worker_waits_for_inflight_usage_before_stopping(self):
        job = self.job()
        self.service.ledger.start(job["id"], 1)
        failed = self.service.ledger.reserve(job["id"], 1, {"n": 1})
        inflight = self.service.ledger.reserve(job["id"], 1, {"n": 2})
        self.service.ledger.finish(failed, error="output_truncated", halt=True)
        stream = asyncio.StreamReader()
        self.service.process = SimpleNamespace(stdout=stream)
        lines = self.service.worker_lines(job["id"])
        stream.feed_data(b'{"type":"progress"}\n')
        self.assertEqual(await anext(lines), b'{"type":"progress"}\n')
        self.service.ledger.finish(inflight, usage={"total_tokens": 100})
        with self.assertRaisesRegex(ValueError, "length"):
            await asyncio.wait_for(anext(lines), timeout=2.5)
        self.assertEqual(self.service.ledger.summary(job["id"])["reported_tokens"], 100)

    async def test_normal_worker_output_and_eof_unchanged(self):
        job = self.job()
        stream = asyncio.StreamReader()
        stream.feed_data(b'first\nsecond\n')
        stream.feed_eof()
        self.service.process = SimpleNamespace(stdout=stream)
        self.assertEqual([line async for line in self.service.worker_lines(job["id"])],
                         [b'first\n', b'second\n'])

    async def test_worker_read_is_released_on_cancellation(self):
        job = self.job()
        stream = asyncio.StreamReader()
        self.service.process = SimpleNamespace(stdout=stream)
        lines = self.service.worker_lines(job["id"])
        task = asyncio.create_task(anext(lines))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(stream._waiter)


if __name__ == "__main__":
    unittest.main()
