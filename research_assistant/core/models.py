"""科研助手使用的稳定数据结构。"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field


@dataclass
class ResearchContext:
    """Runtime Context：调用者提供的研究背景，不保存到对话消息里。"""

    research_topic: str
    weekly_hours: float
    language: str = "中文"
    mode: Literal["answer", "plan"] = "answer"


class Citation(BaseModel):
    """回答所依据的文献证据。"""

    source: str = Field(description="文献文件名")
    page: int | None = Field(description="PDF 页码；非 PDF 文档为 null")
    quote: str = Field(description="检索结果中的原文摘录")


class ResearchTask(BaseModel):
    """可执行、可保存、后续可更新状态的科研任务。"""

    task_id: str = Field(description="任务编号，例如 T1")
    title: str
    purpose: str = Field(description="为什么要做这项任务")
    evidence: list[str] = Field(description="支持该任务的文献依据")
    priority: Literal["high", "medium", "low"]
    estimated_hours: float = Field(gt=0)
    dependencies: list[str] = Field(description="依赖的 task_id，没有则为空列表")
    status: Literal["todo", "doing", "done", "blocked"] = "todo"


class ResearchResponse(BaseModel):
    """Agent 的最终输出；问答和计划共用一个结构。"""

    mode: Literal["answer", "plan"]
    summary: str
    citations: list[Citation]
    status: Literal["ok", "degraded"] = "ok"
    warning: str | None = None
    tasks: list[ResearchTask] = Field(
        default_factory=list,
        description="mode=plan 时填写；普通问答保持空列表",
    )
