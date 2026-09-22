"""科研助手命令行入口。"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from research_assistant.retrieval.knowledge_base import LiteratureIndex
from research_assistant.core.models import ResearchContext, ResearchResponse
from research_assistant.agents.research_agent import build_agent, run_agent


from research_assistant.core.paths import PROJECT_ROOT
# 优先使用项目自己的 .env；没有时兼容现有 LangChain 学习环境。
for env_path in (
    PROJECT_ROOT / ".env",
    PROJECT_ROOT.parent / ".env",
    PROJECT_ROOT.parent / "langchain" / ".env",
):
    if env_path.exists():
        load_dotenv(env_path)
        break
sys.stdout.reconfigure(encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本地科研文献与计划助手")
    parser.add_argument(
        "--library",
        type=Path,
        default=PROJECT_ROOT / "library",
        help="包含 PDF、Markdown、TXT 的文献目录",
    )
    parser.add_argument("--question", help="要询问或规划的科研问题")
    parser.add_argument("--topic", default="当前科研项目", help="研究主题")
    parser.add_argument("--hours", type=float, default=10, help="每周可用小时数")
    parser.add_argument(
        "--backend",
        choices=["tfidf", "bge", "hybrid", "qdrant", "multimodal"],
        default="multimodal",
        help="文献检索后端；当前环境推荐 multimodal",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        help="最低相关度 0 到 1；不填写时使用检索后端默认值",
    )
    parser.add_argument(
        "--save-plan",
        action="store_true",
        help="如果生成了计划，将其保存到 plans 目录",
    )
    return parser.parse_args()


def show_update(node_name: str) -> None:
    """Streaming 只显示对学习有意义的关键节点。"""
    if node_name == "tools":
        print("已完成文献检索。")
    elif node_name == "model":
        print("已完成一轮模型分析。")
    elif node_name == "fallback":
        print("结构化输出解析失败，正在生成普通文本答案。")


def print_response(response: ResearchResponse) -> None:
    if response.warning:
        print(f"\n注意：{response.warning}")
    print(f"\n{response.summary}")

    if response.citations:
        print("\n参考证据：")
        for citation in response.citations:
            location = (
                f"第 {citation.page} 页" if citation.page is not None else "无页码"
            )
            print(f"- {citation.source}，{location}：{citation.quote}")

    if response.tasks:
        print("\n研究任务：")
        for task in response.tasks:
            dependencies = ", ".join(task.dependencies) or "无"
            print(
                f"- [{task.task_id}] {task.title} | {task.priority} | "
                f"{task.estimated_hours} 小时 | 依赖：{dependencies}"
            )
            print(f"  目的：{task.purpose}")


def save_plan(response: ResearchResponse) -> Path:
    """每次生成新文件，不覆盖以前的研究计划。"""
    output_dir = PROJECT_ROOT / "plans"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"research_plan_{timestamp}.json"
    output_path.write_text(response.model_dump_json(indent=2), encoding="utf-8")
    return output_path


def main() -> None:
    args = parse_args()
    question = args.question or input("请输入科研问题：").strip()
    if not question:
        raise ValueError("问题不能为空")

    index = LiteratureIndex(args.library, backend=args.backend)
    agent = build_agent(index, min_score=args.min_score)
    response = run_agent(
        agent,
        question,
        ResearchContext(
            research_topic=args.topic,
            weekly_hours=args.hours,
            mode="plan" if args.save_plan else "answer",
        ),
        on_update=show_update,
    )
    print_response(response)

    if args.save_plan and response.mode == "plan":
        print(f"\n计划已保存：{save_plan(response)}")


if __name__ == "__main__":
    main()
