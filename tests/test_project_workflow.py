"""Project memory and durable approvals tested without model calls or user data."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from research_assistant.web.project_api import make_project_routes
from research_assistant.projects.project_store import Conflict, PlanDraft, ProjectStore, review_tasks
from research_assistant.projects.project_workflow import ProjectWorkflow, make_drafter, source_versions


def plan():
    return {"plan": {"summary": "测试研究计划", "tasks": [
        {"task_id": "T1", "title": "定义实验指标", "estimated_hours": 2},
        {"task_id": "T2", "title": "运行基线对照", "estimated_hours": 3, "dependencies": ["T1"]}]}, "evidence": []}


class ProjectWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "library").mkdir()
        self.store = ProjectStore(self.root / ".data/projects.sqlite3")
        self.project = self.store.create({"name": "测试项目", "goal": "验证研究流程", "weekly_hours": 10})
        self.drafter = Mock(side_effect=lambda p, r: copy.deepcopy(plan()))
        self.workflow = ProjectWorkflow(self.root, self.store, self.drafter)

    def reserve(self):
        p = self.store.get(self.project["id"])
        return self.store.reserve(p["id"], p["revision"], "制定下一步计划", source_versions(self.root, p["sources"]))

    def test_interrupt_restart_approval_does_not_redraft(self):
        run = self.reserve()
        paused = self.workflow.execute(run["id"])
        self.assertEqual(paused["phase"], "awaiting_approval")
        self.assertEqual(self.store.get(self.project["id"])["tasks"], [])
        restarted = ProjectWorkflow(self.root, ProjectStore(self.store.path), Mock(side_effect=AssertionError("must not generate")))
        result = restarted.execute(run["id"], "approve")
        self.assertEqual(result["phase"], "applied")
        tasks = self.store.get(self.project["id"])["tasks"]
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[1]["dependencies"], [tasks[0]["task_id"]])
        self.assertEqual(self.drafter.call_count, 1)
        restarted.execute(run["id"], "approve")
        self.assertEqual(len(self.store.get(self.project["id"])["tasks"]), 2)

    def test_rejection_keeps_project_revision_and_tasks(self):
        run = self.reserve()
        self.workflow.execute(run["id"])
        self.assertEqual(self.workflow.execute(run["id"], "reject")["phase"], "rejected")
        self.assertEqual(self.store.get(self.project["id"]), self.project)

    def test_old_draft_cannot_overwrite_project_updates(self):
        run = self.reserve()
        self.workflow.execute(run["id"])
        settings = {k: self.project[k] for k in ("name", "goal", "weekly_hours", "notes", "sources", "archived")}
        settings["notes"] = "新进展"
        self.store.update(self.project["id"], 1, settings)
        self.assertEqual(self.workflow.execute(run["id"], "approve")["phase"], "stale")
        self.assertEqual(self.store.get(self.project["id"])["tasks"], [])

    def test_source_change_prevents_apply(self):
        path = self.root / "library/paper.md"
        path.write_text("fixture v1", encoding="utf-8")
        settings = {k: self.project[k] for k in ("name", "goal", "weekly_hours", "notes", "sources", "archived")}
        settings["sources"] = ["paper.md"]
        self.store.update(self.project["id"], 1, settings)
        run = self.reserve()
        self.workflow.execute(run["id"])
        path.write_text("fixture v2", encoding="utf-8")
        self.assertEqual(self.workflow.execute(run["id"], "approve")["phase"], "stale")

    def test_failed_generation_explicit_retry_then_pause(self):
        run = self.reserve()
        self.drafter.side_effect = [RuntimeError("offline test failure"), plan()]
        with self.assertRaises(RuntimeError):
            self.workflow.execute(run["id"])
        self.store.mark(run["id"], phase="failed")
        self.assertEqual(self.workflow.execute(run["id"])["phase"], "awaiting_approval")
        self.assertEqual(self.drafter.call_count, 2)

    def test_draft_memo_recovers_checkpoint_gap_without_call(self):
        run = self.reserve()
        draft = PlanDraft.model_validate(plan()["plan"]).model_dump()
        self.store.mark(run["id"], phase="awaiting_approval", draft=draft)
        self.drafter.side_effect = AssertionError("cached draft must be reused")
        self.assertEqual(self.workflow.execute(run["id"], "approve")["phase"], "applied")

    def test_over_budget_stops_before_approval(self):
        run = self.reserve()
        result = plan()
        result["plan"]["tasks"][0]["estimated_hours"] = 20
        self.drafter.side_effect = lambda p, r: result
        self.assertEqual(self.workflow.execute(run["id"])["phase"], "blocked")
        self.assertTrue(self.store.proposal(run["id"])["issues"])
        self.assertEqual(self.store.get(self.project["id"])["tasks"], [])

    def test_duplicate_missing_dependency_cycle_and_nonfinite(self):
        for changes in ({"dependencies": ["missing"]}, {"dependencies": ["T2"]}, {"title": "运行基线对照"}, {"task_id": "T2"}):
            draft = PlanDraft.model_validate(plan()["plan"]).model_dump()
            draft["tasks"][0].update(changes)
            self.assertTrue(review_tasks(self.project, draft["tasks"]))
        bad = plan()["plan"]
        bad["tasks"][0]["estimated_hours"] = float("nan")
        with self.assertRaises(ValidationError):
            PlanDraft.model_validate(bad)

    def test_manual_tasks_progress_dependencies_and_stale_edit(self):
        p = self.store.add_task(self.project["id"], 1, {"title": "准备数据", "estimated_hours": 2})
        t1 = p["tasks"][0]["task_id"]
        p = self.store.add_task(p["id"], p["revision"], {"title": "验证数据", "estimated_hours": 2, "dependencies": [t1]})
        t2 = p["tasks"][1]["task_id"]
        with self.assertRaises(Conflict):
            self.store.update_task(p["id"], p["revision"], t2, "doing", "")
        p = self.store.update_task(p["id"], p["revision"], t1, "done", "已验证")
        p = self.store.update_task(p["id"], p["revision"], t2, "doing", "开始验证")
        with self.assertRaises(Conflict):
            self.store.update_task(p["id"], p["revision"], t1, "todo", "")
        with self.assertRaises(Conflict):
            self.store.update_task(p["id"], 1, t2, "done", "")
        self.assertEqual(ProjectStore(self.store.path).get(p["id"])["tasks"][1]["progress_note"], "开始验证")

    def test_one_pending_proposal_and_project_isolation(self):
        self.reserve()
        with self.assertRaises(Conflict):
            self.reserve()
        other = self.store.create({"name": "另一个项目", "goal": "隔离", "weekly_hours": 4})
        self.store.reserve(other["id"], 1, "另一个工作流", {})
        self.assertEqual(other["tasks"], [])

    def test_source_validation(self):
        with self.assertRaises(ValueError):
            source_versions(self.root, ["../projects.sqlite3"])
        with self.assertRaises(ValueError):
            source_versions(self.root, ["missing.pdf"])

    def test_parser_cache_change_invalidates_source_snapshot(self):
        from research_assistant.ingestion.mineru_extractor import MinerUExtractor
        path = self.root / "library/paper.pdf"
        path.write_bytes(b"synthetic fixture")
        first = source_versions(self.root, ["paper.pdf"])
        extractor = MinerUExtractor(cache_dir=self.root / ".cache/mineru")
        cache = extractor.cache_path(path)
        cache.mkdir(parents=True)
        (cache / "manifest.json").write_text("{}")
        (cache / "paper_content_list.json").write_text("[]")
        second = source_versions(self.root, ["paper.pdf"])
        self.assertEqual(first["paper.pdf"]["file"], second["paper.pdf"]["file"])
        self.assertNotEqual(first, second)

    def test_planner_receives_explicit_memory_and_bounded_single_call(self):
        import json
        p = self.store.add_task(self.project["id"], 1, {"title": "已有任务", "estimated_hours": 2})
        p = self.store.update_task(p["id"], p["revision"], p["tasks"][0]["task_id"], "blocked", "测试数据尚未准备")
        model, index = Mock(), Mock(side_effect=AssertionError("no sources selected"))
        model.with_structured_output.return_value.invoke.return_value = PlanDraft.model_validate(plan()["plan"])
        with patch('research_assistant.projects.project_workflow.build_chat_model', return_value=model):
            make_drafter(index)(p, "根据受阻状态安排下一步")
        self.assertEqual(model.max_tokens, 3500)
        invoke = model.with_structured_output.return_value.invoke
        self.assertEqual(invoke.call_count, 1)
        memory = json.loads(invoke.call_args.args[0][1].content)["project_memory"]
        self.assertEqual(memory["remaining_hours"], 8)
        self.assertEqual(memory["open_tasks"][0]["progress_note"], "测试数据尚未准备")

    def test_applied_transaction_survives_later_checkpoint_failure(self):
        run = self.reserve()
        self.workflow.execute(run["id"])
        finish = self.store.finish
        def commit_then_fail(*args):
            finish(*args)
            raise RuntimeError("simulated crash after commit")
        with patch.object(self.store, "finish", side_effect=commit_then_fail):
            with self.assertRaises(RuntimeError):
                self.workflow.execute(run["id"], "approve")
        self.assertEqual(self.workflow.execute(run["id"], "approve")["phase"], "applied")
        self.assertEqual(len(self.store.get(self.project["id"])["tasks"]), 2)

    def test_archived_project_prevents_mutation(self):
        settings = {k: self.project[k] for k in ("name", "goal", "weekly_hours", "notes", "sources", "archived")}
        settings["archived"] = True
        p = self.store.update(self.project["id"], 1, settings)
        with self.assertRaises(Conflict):
            self.store.add_task(p["id"], p["revision"], {"title": "禁止添加", "estimated_hours": 1})
        with self.assertRaises(Conflict):
            self.reserve()

    def test_api_permission_validation_and_background_approval(self):
        app = Starlette(routes=make_project_routes(self.root, Mock(), drafter=self.drafter))
        with TestClient(app) as client:
            url = f"/api/projects/{self.project['id']}/proposals"
            self.assertEqual(client.post(url, json={"revision": 1, "request": "下一步"}).status_code, 403)
            self.assertEqual(client.post("/api/projects", json={}, headers={"Origin": "https://untrusted.example"}).status_code, 403)
            self.assertEqual(client.post("/api/projects", json=[]).status_code, 400)
            self.assertEqual(client.post("/api/projects", json={"name": "", "goal": "x", "weekly_hours": 1}).status_code, 400)
            self.assertEqual(client.get("/api/projects/missing").status_code, 404)
            response = client.post(url, json={"revision": 1, "request": "下一步", "allow_external": True})
            self.assertEqual(response.status_code, 202)
            proposal_id = response.json()["id"]
            import time
            for _ in range(100):
                row = client.get("/api/project-proposals/" + proposal_id).json()
                if not row["active"]:
                    break
                time.sleep(.02)
            self.assertEqual(row["phase"], "awaiting_approval")
            self.assertEqual(client.post("/api/project-proposals/" + proposal_id, json={"action": "approve"}).status_code, 202)
            for _ in range(100):
                row = client.get("/api/project-proposals/" + proposal_id).json()
                if not row["active"]:
                    break
                time.sleep(.02)
            self.assertEqual(row["phase"], "applied")
            self.assertNotIn("project", row)
            self.assertEqual(self.drafter.call_count, 1)


if __name__ == "__main__":
    unittest.main()
