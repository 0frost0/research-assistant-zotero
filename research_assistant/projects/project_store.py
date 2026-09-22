"""Explicit project memory and atomic task changes; separate from graph checkpoints."""

import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def now():
    return datetime.now(timezone.utc).isoformat()


class Conflict(ValueError):
    pass


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=3000)
    weekly_hours: float = Field(ge=1, le=80)
    notes: str = Field(default="", max_length=4000)
    sources: list[str] = Field(default_factory=list, max_length=50)
    archived: bool = False


class TaskInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)
    title: str = Field(min_length=1, max_length=200)
    purpose: str = Field(default="", max_length=1500)
    estimated_hours: float = Field(gt=0, le=80)
    priority: Literal["high", "medium", "low"] = "medium"
    dependencies: list[str] = Field(default_factory=list, max_length=30)
    evidence: list[str] = Field(default_factory=list, max_length=10)


class DraftTask(TaskInput):
    task_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")


class PlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    summary: str = Field(min_length=1, max_length=5000)
    tasks: list[DraftTask] = Field(min_length=1, max_length=12)


ACTIVE_PHASES = {"queued", "drafting", "checking", "awaiting_approval", "failed"}
TERMINAL_PHASES = {"applied", "rejected", "blocked", "stale"}


def review_tasks(project, tasks):
    """Rules inspect the entire task DAG and budget; this is not an LLM reviewer."""
    issues = []
    combined = project["tasks"] + tasks
    ids = [t["task_id"] for t in combined]
    if len(ids) != len(set(ids)):
        issues.append("任务编号重复或与已有任务冲突。")
    titles = [t["title"].strip().casefold() for t in combined]
    if len(titles) != len(set(titles)):
        issues.append("任务标题与已有任务或本次草案重复。")
    if len(combined) > 100:
        issues.append("单个项目最多保留 100 项任务，请为新阶段创建项目。")
    known = set(ids)
    if any(d not in known for t in combined for d in t["dependencies"]):
        issues.append("存在不存在的依赖任务。")
    try:
        tuple(TopologicalSorter({t["task_id"]: t["dependencies"] for t in combined}).static_order())
    except CycleError:
        issues.append("任务依赖存在循环。")
    hours = sum(t["estimated_hours"] for t in combined if t.get("status", "todo") != "done")
    if hours > project["weekly_hours"] + 1e-6:
        issues.append(f"未完成任务预计共 {hours:g} 小时，超过当前 {project['weekly_hours']:g} 小时预算。")
    return issues


class ProjectStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as con:
            con.execute("CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS proposals (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS project_events (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, created_at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL)")

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path, timeout=15)) as con:
            with con:
                con.execute("BEGIN IMMEDIATE")
                yield con

    def _read(self, con, table, item_id):
        if table not in {"projects", "proposals"}:
            raise ValueError("Unknown record type")
        row = con.execute(f"SELECT payload FROM {table} WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise KeyError("记录不存在")
        return json.loads(row[0])

    def _save(self, con, table, value):
        if table not in {"projects", "proposals"}:
            raise ValueError("Unknown record type")
        con.execute(f"UPDATE {table} SET payload=? WHERE id=?", (json.dumps(value, ensure_ascii=False), value["id"]))

    def _event(self, con, project_id, kind, detail):
        con.execute("INSERT INTO project_events(project_id,created_at,kind,detail) VALUES(?,?,?,?)",
                    (project_id, now(), kind, detail))

    def get(self, project_id):
        with closing(sqlite3.connect(self.path)) as con:
            return self._read(con, "projects", project_id)

    def proposal(self, proposal_id):
        with closing(sqlite3.connect(self.path)) as con:
            return self._read(con, "proposals", proposal_id)

    def list(self):
        with closing(sqlite3.connect(self.path)) as con:
            projects = [json.loads(r[0]) for r in con.execute("SELECT payload FROM projects")]
        return sorted(projects, key=lambda p: p["updated_at"], reverse=True)

    def detail(self, project_id):
        with closing(sqlite3.connect(self.path)) as con:
            project = self._read(con, "projects", project_id)
            proposals = [json.loads(r[0]) for r in con.execute("SELECT payload FROM proposals WHERE project_id=? ORDER BY created_at DESC LIMIT 30", (project_id,))]
            events = [{"at": r[0], "kind": r[1], "detail": r[2]} for r in con.execute("SELECT created_at,kind,detail FROM project_events WHERE project_id=? ORDER BY id DESC LIMIT 60", (project_id,))]
        return {"project": project, "proposals": proposals, "events": events}

    def create(self, values):
        value = ProjectInput.model_validate(values).model_dump()
        value.update(id=uuid.uuid4().hex, revision=1, tasks=[], created_at=now(), updated_at=now())
        with self.transaction() as con:
            con.execute("INSERT INTO projects VALUES(?,?)", (value["id"], json.dumps(value, ensure_ascii=False)))
            self._event(con, value["id"], "project_created", "创建项目")
        return value

    def _current(self, con, project_id, revision):
        p = self._read(con, "projects", project_id)
        if p["revision"] != revision:
            raise Conflict("项目已变化，请刷新后重试；未覆盖已有修改。")
        return p

    def _write_project(self, con, project, kind, detail):
        project["revision"] += 1
        project["updated_at"] = now()
        self._save(con, "projects", project)
        self._event(con, project["id"], kind, detail)
        return project

    def update(self, project_id, revision, values):
        data = ProjectInput.model_validate(values).model_dump()
        with self.transaction() as con:
            p = self._current(con, project_id, revision)
            p.update(data)
            return self._write_project(con, p, "project_updated", "更新项目设置或记忆")

    def add_task(self, project_id, revision, values):
        t = TaskInput.model_validate(values).model_dump()
        t.update(task_id=uuid.uuid4().hex, status="todo", progress_note="", created_at=now())
        with self.transaction() as con:
            p = self._current(con, project_id, revision)
            if p["archived"]:
                raise Conflict("项目已归档，请先恢复。")
            issues = review_tasks(p, [t])
            if issues:
                raise ValueError(" ".join(issues))
            p["tasks"].append(t)
            return self._write_project(con, p, "task_added", t["title"])

    def update_task(self, project_id, revision, task_id, status, note):
        if status not in {"todo", "doing", "done", "blocked"} or not isinstance(note, str) or len(note) > 2000:
            raise ValueError("任务状态或进展备注无效。")
        with self.transaction() as con:
            p = self._current(con, project_id, revision)
            if p["archived"]:
                raise Conflict("项目已归档，请先恢复。")
            tasks = {t["task_id"]: t for t in p["tasks"]}
            if task_id not in tasks:
                raise KeyError("任务不存在")
            task = tasks[task_id]
            if status in {"doing", "done"} and any(tasks[d]["status"] != "done" for d in task["dependencies"]):
                raise Conflict("依赖任务尚未完成，不能开始或完成此任务。")
            if task["status"] == "done" and status != "done" and any(task_id in t["dependencies"] and t["status"] in {"doing", "done"} for t in tasks.values()):
                raise Conflict("已有下游任务开始或完成，请先调整下游状态。")
            task.update(status=status, progress_note=note.strip())
            return self._write_project(con, p, "task_updated", f"{task['title']} -> {status}")

    def reserve(self, project_id, revision, request, versions):
        if not isinstance(request, str) or not 1 <= len(request.strip()) <= 3000:
            raise ValueError("规划要求应为 1 到 3000 个字符。")
        with self.transaction() as con:
            p = self._current(con, project_id, revision)
            if p["archived"]:
                raise Conflict("项目已归档，请先恢复。")
            existing = [json.loads(r[0]) for r in con.execute("SELECT payload FROM proposals WHERE project_id=?", (project_id,))]
            if any(r["phase"] in ACTIVE_PHASES for r in existing):
                raise Conflict("此项目已有未处理工作流，请先审批、重试或放弃。")
            run = {"id": uuid.uuid4().hex, "project_id": project_id, "base_revision": revision,
                   "project": p, "request": request.strip(), "source_versions": versions,
                   "phase": "queued", "created_at": now(), "updated_at": now(), "issues": [],
                   "draft": None, "evidence": [], "error": None, "external_consent": True}
            con.execute("INSERT INTO proposals VALUES(?,?,?,?)", (run["id"], project_id, run["created_at"], json.dumps(run, ensure_ascii=False)))
            self._event(con, project_id, "planning_started", run["id"])
        return run

    def mark(self, proposal_id, **values):
        with self.transaction() as con:
            r = self._read(con, "proposals", proposal_id)
            if r["phase"] in TERMINAL_PHASES:
                return r
            r.update(values, updated_at=now())
            self._save(con, "proposals", r)
            return r

    def finish(self, proposal_id, decision, versions):
        """Approval and task insertion share one transaction and an idempotency key."""
        with self.transaction() as con:
            r = self._read(con, "proposals", proposal_id)
            if r["phase"] in TERMINAL_PHASES:
                return r
            p = self._read(con, "projects", r["project_id"])
            if decision == "reject":
                r["phase"] = "rejected"
            elif decision != "approve":
                raise ValueError("无效的审批决定。")
            elif p["revision"] != r["base_revision"] or p["archived"] or versions != r["source_versions"]:
                r.update(phase="stale", error="项目或资料版本已变化，草案未应用，请重新规划。")
            else:
                draft = PlanDraft.model_validate(r["draft"]).model_dump()
                issues = review_tasks(p, draft["tasks"])
                if issues:
                    r.update(phase="blocked", issues=issues)
                else:
                    aliases = {t["task_id"]: f"{proposal_id}_{t['task_id']}" for t in draft["tasks"]}
                    for t in draft["tasks"]:
                        t["task_id"] = aliases[t["task_id"]]
                        t["dependencies"] = [aliases.get(d, d) for d in t["dependencies"]]
                        t.update(status="todo", progress_note="", created_at=now(), proposal_id=proposal_id)
                    p["tasks"].extend(draft["tasks"])
                    self._write_project(con, p, "plan_applied", proposal_id)
                    r["phase"] = "applied"
            r["updated_at"] = now()
            self._save(con, "proposals", r)
            if r["phase"] != "applied":
                self._event(con, p["id"], r["phase"], proposal_id)
            return r
