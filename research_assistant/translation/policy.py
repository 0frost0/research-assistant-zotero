"""Shared, credential-free translation policy used by app and isolated worker."""
from urllib.parse import urlsplit


def runtime_policy(model, base_url):
    deepseek_v4 = (
        urlsplit(base_url).hostname == "api.deepseek.com"
        and model.startswith("deepseek-v4-")
    )
    return {
        "adapter_version": 3,
        "qps": 2,
        "workers": 4,
        "request_timeout": 90,
        "request_attempts": 3,
        "thinking": "disabled" if deepseek_v4 else "provider_default",
        "max_output_tokens": 2048,
    }


def engine_settings(request):
    # Import only in the pinned worker environment, not the web application.
    from pdf2zh_next.config.translate_engine_model import DeepSeekSettings, OpenAISettings

    policy = runtime_policy(request["model"], request["base_url"])
    if policy["thinking"] == "disabled":
        result = DeepSeekSettings(
            deepseek_model=request["model"],
            deepseek_api_key=request["key"],
            deepseek_thinking_mode="disabled",
        ).transform()
        # Keep the user's configured endpoint, never redirect to a new host.
        result.openai_base_url = request["base_url"]
    else:
        result = OpenAISettings(
            openai_model=request["model"],
            openai_api_key=request["key"],
            openai_base_url=request["base_url"],
        )
    result.openai_timeout = str(policy["request_timeout"])
    return result


ERROR_MESSAGES = {
    "repeated_request": "检测到同一模型请求反复发送，已停止本次翻译，避免循环消耗。已完成段落缓存与实际用量保留。",
    "consecutive_failures": "模型请求连续失败，已停止本次翻译，避免继续消耗。请检查服务状态后手动重试。",
    "budget_exhausted": "已达到单篇或今日全文翻译预算，停止新请求。已完成段落保留在引擎缓存中；重试不会清零消耗。",
    "incomplete_response": "模型输出被截断或为空，已停止后续请求，未发布完整译文。已返回用量保留。",
    "output_truncated": "模型返回 length，输出被截断，未发布完整译文。请先核验模型输出限制，再重试；已返回用量保留。",
    "empty_response": "模型正常结束但未返回有效译文，未发布完整译文。请先核验模型接口与推理配置；已返回用量保留。",
    "content_filtered": "模型服务过滤了响应，未发布完整译文。已停止后续请求并保留已返回用量。",
    "invalid_response": "模型响应缺少有效结果或结束原因异常，未发布完整译文。请先核验接口兼容性；已返回用量保留。",
    "cancelled": "翻译已停止，已发请求可能计费；原件、缓存和笔记保留。",
    "insufficient_balance": "模型服务账户余额不足，翻译已停止。请充值后手动重试；不会自动重复扣费请求。",
    "authentication_failed": "模型 API Key 无效或已失效。请更新翻译配置后重试。",
    "permission_denied": "模型服务拒绝访问，请检查账号权限及模型是否可用。",
    "rate_limited": "模型服务限流，已停止有限重试。请稍后重试。",
    "request_timeout": "模型请求超时，有限重试仍未完成。请检查服务状态后重试。",
    "connection_failed": "无法连接模型服务，请检查网络或服务地址后重试。",
    "invalid_model_request": "模型或接口参数不可用，请检查翻译模型和服务地址。",
    "service_unavailable": "模型服务暂时不可用，请稍后手动重试。",
    "partial_translation": "部分段落翻译失败，未发布完整译文。请查看任务诊断后重试；原件和旧笔记保留。",
}


def error_code(status=None, exception_name=""):
    if status in (400, 404, 422):
        return "invalid_model_request"
    if status == 401:
        return "authentication_failed"
    if status == 402:
        return "insufficient_balance"
    if status == 403:
        return "permission_denied"
    if status == 429:
        return "rate_limited"
    if isinstance(status, int) and status >= 500:
        return "service_unavailable"
    if exception_name in {"APITimeoutError", "ReadTimeout", "ConnectTimeout", "TimeoutException"}:
        return "request_timeout"
    if exception_name in {"APIConnectionError", "ConnectError"}:
        return "connection_failed"
    return "engine_failed"
