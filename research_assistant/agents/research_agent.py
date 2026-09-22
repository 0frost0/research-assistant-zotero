"""科研 Agent：用检索工具取得证据，再生成结构化问答或计划。"""

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from research_assistant.retrieval.knowledge_base import LiteratureIndex
from research_assistant.core.models import ResearchContext, ResearchResponse


def research_prompt(context: ResearchContext) -> str:
    """根据本次科研主题和时间预算生成系统提示词。"""
    return f"""你是严谨的科研文献与计划助手。

            当前研究主题：{context.research_topic}
            每周可用时间：{context.weekly_hours} 小时
            输出语言：{context.language}

            规则：
            1. 检索已经由程序完成，直接根据用户消息中的检索证据生成最终答案，不要请求再次检索。
            2. 只能使用检索证据，不得虚构论文结论、来源、页码或原文。
            3. citations 中的 source、page、quote 必须直接来自检索证据。
            4. 用户请求解释或总结时，mode 使用 answer，tasks 保持空列表。
            5. 用户请求计划时，mode 使用 plan；任务总工时不得明显超过每周时间预算。
            6. 每个计划任务都要具体、可执行，并说明文献依据和任务依赖。
            7. QUALITY 是证据质量限制，必须遵守。图片仅提供出处且 QUOTE 为空时，你并未看到图像，不得编造图中数值、连线或图片原文引用；可以说明已定位图片，结合其他正文解释。
            8. 自动提取的表格数值和单位未独立核验，涉及精确数值时要明确来源限制，不把自动提取等同人工校对。
            """


@dataclass
class ResearchAgentBundle:
    """保存检索函数、结构化模型和普通文本备用模型。"""

    retrieve: Callable[[str], str]
    responder: Any
    model: Any
    search_history: list[dict[str, Any]]


def build_agent(
    index: LiteratureIndex,
    *,
    sources: list[str] | None = None,
    file_types: list[str] | None = None,
    min_score: float | None = None,
) -> ResearchAgentBundle:
    """创建固定执行路径，避免模型反复决定是否调用检索工具。"""

    search_history: list[dict[str, Any]] = []

    def retrieve(query: str) -> str:
        results = index.search_results(
            query,
            sources=sources,
            file_types=file_types,
            min_score=min_score,
        )
        search_history.append(
            {
                "query": query,
                "results": [result.model_dump() for result in results],
            }
        )
        return index.format_results(results)

    model = build_chat_model()
    responder = model.with_structured_output(
        ResearchResponse,
        method="function_calling",
    )
    return ResearchAgentBundle(
        retrieve=retrieve,
        responder=responder,
        model=model,
        search_history=search_history,
    )


def build_chat_model() -> ChatOpenAI:
    """问答和项目规划共用连接、思考模式与超时配置。"""
    base_url = os.environ["OPENAI_BASE_URL"]
    model_options = {}
    if "deepseek.com" in base_url.lower():
        # DeepSeek 思考模式与强制结构化函数调用不兼容。
        model_options["extra_body"] = {"thinking": {"type": "disabled"}}

    return ChatOpenAI(
        model=os.environ["OPENAI_MODEL"],
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=base_url,
        temperature=0,
        # 单次模型请求也必须有上限，避免网络半开连接长期占住网页请求。
        timeout=float(os.getenv("RESEARCH_ASSISTANT_MODEL_TIMEOUT", "30")),
        # 连接失败应尽快返回清晰错误；需要重试时由上层按错误类型控制。
        max_retries=0,
        **model_options,
    )


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(part for part in parts if part).strip()
    return ""


def _answer_messages(
    question: str,
    evidence: str,
    context: ResearchContext,
    *,
    structured: bool,
) -> list[Any]:
    output_instruction = (
        "现在直接提交最终的 ResearchResponse。"
        if structured
        else "请直接给出简洁的中文答案，并在末尾列出使用的文件名和页码。"
    )
    return [
        SystemMessage(content=research_prompt(context)),
        HumanMessage(
            content=(
                f"用户问题：\n{question}\n\n"
                f"本地检索证据：\n{evidence}\n\n"
                f"{output_instruction}"
            )
        ),
    ]


RECOVERABLE_OUTPUT_ERRORS = (OutputParserException, ValidationError)


def _local_evidence_fallback(
    agent: ResearchAgentBundle,
    context: ResearchContext,
    warning: str,
) -> ResearchResponse:
    """模型没有可显示文本时，至少把最相关的本地证据返回给用户。"""
    results = agent.search_history[0]["results"] if agent.search_history else []
    if not results:
        summary = "资料库中没有找到达到当前相关度阈值的证据。"
    else:
        first = results[0]
        location = f"第 {first['page']} 页" if first.get("page") else "无页码"
        summary = (
            f"模型未能生成答案。本地检索到的最相关证据来自 "
            f"{first['source']}（{location}）：{first['quote'][:600]}"
        )
    return ResearchResponse(
        mode=context.mode,
        summary=summary,
        citations=[],
        tasks=[],
        status="degraded",
        warning=warning,
    )


def run_agent(
    agent: ResearchAgentBundle,
    question: str,
    context: ResearchContext,
    on_update: Callable[[str], None] | None = None,
) -> ResearchResponse:
    """只检索一次并生成答案；结构化失败时用普通文本兜底。"""
    evidence = agent.retrieve(question)
    if on_update is not None:
        on_update("tools")

    if evidence.startswith("NO_RELIABLE_EVIDENCE"):
        return ResearchResponse(
            mode=context.mode,
            summary="资料库中没有找到足够可靠的证据，暂时无法基于现有资料回答。",
            citations=[],
            tasks=[],
        )

    try:
        response = agent.responder.invoke(
            _answer_messages(question, evidence, context, structured=True)
        )
        if on_update is not None:
            on_update("model")
        if isinstance(response, ResearchResponse):
            return response
        return ResearchResponse.model_validate(response)
    except RECOVERABLE_OUTPUT_ERRORS as exc:
        if on_update is not None:
            on_update("fallback")
        plain_response = agent.model.invoke(
            _answer_messages(question, evidence, context, structured=False)
        )
        summary = _message_text(plain_response)
        if summary:
            return ResearchResponse(
                mode=context.mode,
                summary=summary,
                citations=[],
                tasks=[],
                status="degraded",
                warning=(
                    "结构化输出解析失败，当前显示普通文本答案；"
                    f"引用未经过结构校验。原因：{type(exc).__name__}。"
                ),
            )
        return _local_evidence_fallback(
            agent,
            context,
            warning=f"模型输出无法解析或显示：{type(exc).__name__}。",
        )
