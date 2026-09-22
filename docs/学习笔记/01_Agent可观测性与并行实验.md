# 第 1 课：为什么给 Agent 加执行记录

本课目标不是背会 Trace，而是能回答：一次对照为什么慢、失败在哪一步、并行改变了什么。先读本课和相邻代码，不要求一次看完全部项目。

## 1. 先提出可验证的问题

已知系统有四个单篇分析员、一个综合员、一个复核员。以前只显示“6 次调用”，看不出角色耗时，也不能证明请求是否重叠。本次增加的是程序执行观测，不是新研究 Agent。

Trace 对应整轮任务；Span 对应其中一个阶段或一次模型调用。父子关系表示“这次模型调用属于哪个阶段”。这不是模型的 chain-of-thought，也没有记录模型内部推理。

## 2. 跟着一次调用走

关键代码在 [comparison_workflow.py](../../research_assistant/agents/comparison_workflow.py) 的 `run()` 内部 `invoke()`。核心顺序如下（省略记录代码）：

```python
async with semaphore:
    calls += 1
    result = await asyncio.wait_for(self.caller(role, payload), self.call_timeout)
```

进入前，`role` 是字符串，例如 `analyst`；`payload` 是字典，单篇分析只包含问题和该篇的 document。semaphore 的剩余名额决定当前协程能否进入。四个分析协程已经由 LangGraph 发起，但并不等于四个请求都已发送。

以并发数 1 为例：A 获得名额、调用模型；B/C/D 在 `async with` 入口等待。A 的 `await` 期间事件循环可运行其他协程，但 B/C/D 仍拿不到名额。A 退出上下文后释放名额，下一篇才发请求。改为 4，则四个请求都可进入等待网络返回的状态。

`calls += 1` 放在获得名额之后，是为了统计实际开始尝试的调用。只排队还没发出就被取消的分支，不能也当成一次模型调用。

`wait_for(..., timeout)` 限制这次请求的等待，超时会取消被等待的协程。它不表示供应商必定停止计费；远端可能已经收到请求。这也是页面保留“已发送请求可能仍计费”的原因。

## 3. 怎么量时间

[agent_telemetry.py](../../research_assistant/core/agent_telemetry.py) 的关键两行：

```python
self.origin = clock()
return max(0, round((self.clock() - self.origin) * 1000, 3))
```

默认 `clock` 是 `time.perf_counter`，返回单调时钟的秒数。假设起点为 100.0，当前为 100.25，相减并乘 1000 得到 250 ms。它用于持续时间，不用于判断今天日期；系统校时不应使耗时突然变成负数。

UTC 日期单独用于“这轮何时开始”。程序重启后旧进程的单调时钟不能直接接着算，所以中断记录保留已完成 Span，未完成的时长标未知，不编造一个成功结束时间。

每次模型 Span 记三个位置：开始排队、获得名额开始请求、收到结果并完成结构校验。

```text
Span 开始 ---- 等待并发名额 ---- 请求开始 ---- 等待模型与结构校验 ---- Span 结束
             queue_ms                       request_duration_ms
<--------------------------- duration_ms ---------------------------->
```

`request_duration_ms` 包含客户端与网络、供应商等待、生成和解析，不能解释成纯 GPU 推理时间；本轮还没有测首 token 时间（TTFT）。阶段 Span 已结束也不代表科学结论正确，甚至可能有子调用失败后走降级路径。

## 4. 为什么失败也有 token

模型可能返回了文本或工具调用，但格式不符合 Pydantic schema。没有 `include_raw` 时，结构化解析失败很容易使调用方拿不到用量。

```python
response = await model.with_structured_output(
    schema, method="function_calling", include_raw=True
).ainvoke(messages)
```

这里返回的是包含 `raw`、`parsed`、`parsing_error` 的字典，不再直接是 schema 实例。`raw` 是原始 AIMessage；`parsed` 是解析后的结构；`parsing_error` 表示解析失败。

项目把它们转成 `ModelReply`：先从 `raw.usage_metadata` 取出用量，交给轨迹；若有 parsing_error 再抛出，让原来的降级逻辑处理。因此能保留“消耗了 token，但输出结构不合法”的真实状态。原始响应全文、完整提示词和异常文本没有放入轨迹，只保存白名单数字与错误类别。

### 不要把未知当零

```python
if values["input_tokens"] + values["output_tokens"] != values["total_tokens"]:
    return None
```

输入是供应商经过 LangChain 规范化后的 usage 字典。各字段必须是非负整数且相互一致，否则返回 None；这里的 None 表示“无法可靠统计”，不是 0。

若四次调用只有三次报告 120 token，总结是“已报告 360 token，覆盖 3/4 次”，不能声称整轮仅花了 360 token。缓存读取是输入 token 的子集，在当前 LangChain 口径下不能再加到 total_tokens 上。没有实际价格与完整用量，本轮不换算人民币费用。

## 5. 从两个开源实现学到什么

### Open Deep Research：并发必须有范围

源码：[deep_researcher.py，固定版本 1b7d2e80](https://github.com/langchain-ai/open_deep_research/blob/1b7d2e80db9faa586165c60e09096dbbfd483a64/src/open_deep_research/deep_researcher.py)，本轮只读检查 `supervisor_tools()` 相关块，不代表审计了整个仓库。

关键语句是：

```python
allowed_conduct_research_calls = conduct_research_calls[:configurable.max_concurrent_research_units]
tool_results = await asyncio.gather(*research_tasks)
```

第一行对 supervisor 提议的研究工具调用列表切片。例如提出 5 项而上限是 2，只选前两项进入本轮执行，其余进入单独的超额错误处理。research_tasks 来自每个已允许任务的 `researcher_subgraph.ainvoke(...)`；gather 等待这些协程，并按传入顺序返回结果。源码随后 `zip(tool_results, allowed_conduct_research_calls)` 将结果与原 tool_call_id 对应，返回 ToolMessage 给 supervisor。

本项目不照搬整个 supervisor 循环：用户选定的 2–4 篇都应被处理，不能随意切掉。我们用 LangGraph Send 创建分支，再以 semaphore 排队控制活跃请求；同时保留固定的综合与复核次数。这是学设计原则，不是抄名字。

### Inspect AI：用量和输出是不同字段

源码：[_model_output.py，固定版本 856f41f1](https://github.com/UKGovernmentBEIS/inspect_ai/blob/856f41f19debe008756ec4c5831f0600c861c51a/src/inspect_ai/model/_model_output.py)，本轮检查 ModelUsage 与 ModelOutput 定义。

```python
input_tokens_cache_read: int | None = Field(default=None)
time: float | None = Field(default=None)
```

第一项属于 ModelUsage，用来区分缓存读取；第二项属于 ModelOutput，是调用耗时。None 表示未给出，并不强行填 0。这个拆分使“生成了什么”“用了多少”“等了多久”可以独立评估。

特别注意：该版本 Inspect 的 input_tokens 注释说明不含缓存，而本项目使用的 LangChain input_tokens 口径包含缓存。不能看到相同字段名就复制计算公式。本项目借鉴分层记录思路，没有引入 Inspect 依赖，也不宣称兼容其完整日志协议。

## 6. 我们实际做了什么实验

运行文件：[run_comparison_experiment.py](../../benchmarks/run_comparison_experiment.py)。默认只用合成证据与固定延迟模拟模型，不读取你的资料库，不发送真实模型请求。

控制条件：4 篇证据、4 个分析角色、一次综合和一次复核，重复 3 次；唯一处理变量是并发数 1 或 4，执行顺序在重复间交替。单篇模拟延迟 150 ms，综合与复核各 40 ms；实际时间还包括调度和结构化处理。另加没有证据的负向控制，两种并发都应为 0 次模型调用。

本轮保存结果：[原始模拟实验 JSON](../../benchmarks/comparison_concurrency_simulated_20260907.json)。

| 四篇合成案例 | 串行（1） | 并行（4） |
| --- | --- | --- |
| 每轮调用数 | 6 | 6 |
| 实测峰值请求并发 | 1 | 4 |
| 3 次耗时中位数 | 724.298 ms | 294.615 ms |
| 3 次的 nearest-rank p95 | 768.726 ms | 299.487 ms |

3 个样本的 p95 实际就是其中的最大值，不是可靠的生产 SLA。两边都是模拟模型，token 用量未知；不能根据这张表写“真实科研任务提速 59%”，也不能推导答案质量提高。它只验证了程序调度、固定调用预算和统计链路。

复现时换一个输出名，不覆盖原记录：

```powershell
& 'D:\miniconda3\envs\deepagents\python.exe' -B -X utf8 `
  benchmarks/run_comparison_experiment.py --mode simulated --repeats 3 `
  --output benchmarks/my_concurrency_experiment.json
```

输出记录数据指纹、prompt 指纹、版本、逐轮 Span 和分组指标，后续脚本版本还增加源码指纹。最早已保存记录没有该字段，不回填假装当时已记录。实验不计时 PDF、Qdrant 或嵌入，只测固定证据后的生成编排。live 模式需要用户显式提供证据快照、外发许可与调用上限，本轮没有执行。

## 7. 轮到你做一小步

先回答：为什么四路并行没有让整轮快四倍？回答时分别提到可以并行的部分、必须串行的部分、额外开销。

然后完成一个小改动：在实验脚本增加“并发数 2”这一组，并让输出同时列出 1、2、4。不要改生产网页的默认值，也不要改 fixture 延迟。提示：既要修改实验的执行组，也要修改结果聚合组；只改其中一处会导致报告缺失。

验收看四件事：三组仍各 6 次调用；峰值并发分别为 1、2、4；无证据控制组仍 0 次；报告保留新文件不覆盖旧实验。耗时不写严格机器断言，Windows 调度和后台负载可能引入抖动。

这一项特意没有由助手代做。你完成后，我们根据代码和结果讨论，再进入会话记忆。掌握的标志是能预测改动效果并解释异常，而不是背下 Semaphore 的定义。
