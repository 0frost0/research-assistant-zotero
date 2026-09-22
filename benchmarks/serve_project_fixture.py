"""Isolated browser QA server: temporary data, simulated planner, no external calls."""

import argparse
import sys
import tempfile
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_assistant.web.project_api import make_project_routes
from research_assistant.projects.project_store import ProjectStore


def fixture_draft(project, request):
    return {"plan": {"summary": "建议先确定评价指标，再开展基线验证。以下任务等待用户确认，尚未执行。", "tasks": [
        {"task_id": "T1", "title": "确定基线实验与评价指标", "purpose": "产出一页指标定义与实验配置，固定数据划分与运行参数。", "estimated_hours": 2, "priority": "high", "dependencies": [], "evidence": []},
        {"task_id": "T2", "title": "执行小规模基线验证", "purpose": "依据已确认的配置记录结果与失败样例，整理下一轮改进问题。", "estimated_hours": 3, "priority": "medium", "dependencies": ["T1"], "evidence": []}]}, "evidence": []}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="research-project-ui-") as temporary:
        root = Path(temporary)
        (root / "library").mkdir()
        ProjectStore(root / ".data/projects.sqlite3").create({"name": "验证项目：科研实验规划", "goal": "使用虚构项目验证计划审批与任务跟进", "weekly_hours": 10, "notes": "仅为隔离测试，未关联真实资料。"})
        async def library(_):
            return JSONResponse({"files": []})
        def no_index(_):
            raise AssertionError("Fixture must not access live retrieval")
        routes = make_project_routes(root, no_index, drafter=fixture_draft, static_dir=ROOT / "research_assistant/web/static")
        routes.extend([Route("/api/library", library), Mount("/static", StaticFiles(directory=ROOT / "research_assistant/web/static"))])
        print("ISOLATED QA: temporary project database and simulated planner; no DeepSeek or Qdrant.", flush=True)
        uvicorn.run(Starlette(routes=routes), host="127.0.0.1", port=args.port)
