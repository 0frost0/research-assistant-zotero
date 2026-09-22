# 从 Translate for Zotero 借鉴按需翻译

**定位修正：划词／段落翻译是补充，不替代整篇中文 PDF。** 全文方向见 [全文翻译调研](Zotero全文翻译调研.md)。

已完成：选区一次请求、持久缓存、来源回跳、待确认草稿、选区用量限制。验证：152 项通过、9 项隔离跳过，8 项模拟浏览器检查通过；未测真实费用或节省比例。

<details>
<summary>展开源码依据、实现细节与复现命令</summary>

核实版本：`windingwind/zotero-pdf-translate` v2.4.7（GitHub latest release，发布于 2026-08-18）。本轮用户确认日常使用的是划词/段落翻译，具体服务尚未提供，因此不能把其“半年一两元”直接当作本项目可达到的价格。

## 源码说明了什么

| 核实点 | 上游证据 | 对本项目的启发 |
| --- | --- | --- |
| 选择文本、批注、标题和摘要的任务 | [task.ts](https://github.com/windingwind/zotero-pdf-translate/blob/v2.4.7/src/utils/task.ts)、[README](https://github.com/windingwind/zotero-pdf-translate/blob/v2.4.7/README.md) | 日常阅读不必默认翻译整篇 PDF |
| GPT 路径发送一条 user 消息，正文来自 data.raw | [gpt.ts](https://github.com/windingwind/zotero-pdf-translate/blob/v2.4.7/src/modules/services/gpt.ts) | 只发当前片段，不带排版标签/批次 JSON 规则，不经过 RAG 或 Agent |
| 默认提示词只要求学术翻译、不要解释 | [prefs.js](https://github.com/windingwind/zotero-pdf-translate/blob/v2.4.7/addon/prefs.js) | 使用简短固定指令，直接接收译文 |
| attachPaperContext 默认 false，可手动开启 | [llmPrompt.ts](https://github.com/windingwind/zotero-pdf-translate/blob/v2.4.7/src/utils/llmPrompt.ts)、prefs.js | 本项目第一版不自动附加论文上下文 |
| 从成功任务队列匹配 raw/service/语言并复用结果 | [services/index.ts](https://github.com/windingwind/zotero-pdf-translate/blob/v2.4.7/src/modules/services/index.ts) | 按文本和服务身份缓存；任务队列缓存不等于永久数据库，本项目另做 SQLite 持久化 |
| 多种传统翻译服务、词典以及可配置 LLM | README、services/ | 费用也取决于所选服务。上游 README 的免费额度和历史价格不是已核实的现行报价 |

插件还能开启多服务对比和附加上下文，因此并非所有配置都省钱；流式输出能改善等待体验，但本身不减少 token。上游任务有失败服务回退，本项目没有照搬自动多服务回退。

## 已实现的阅读闭环

原文 PDF 选择一句/一段 → 手动点“翻译选区” → 优先查本地缓存 → 未命中则一次直接模型请求 → 侧栏原文/机器译文 → 可返回原文选区，或保存为 AI 草稿 → 用户确认后进入笔记检索。

原文版式一直保持原 PDF，不需要把中文重新塞进原文本框。这是按需侧栏翻译，**不是新的一份保留版式中文 PDF**。原来的整篇 PDF 翻译功能保留为可选入口，新上传的自动整篇翻译默认关闭；之前显式保存的用户开关仍被尊重。本机交付前检查未保存自动开关，因此新默认会生效。

| 模块 | 实现 |
| --- | --- |
| translation/selection.py | 选区校验、固定短提示词、直接 OpenAI-compatible SDK 请求、SQLite 缓存、用量预留与草稿来源 |
| web/reading_api.py | selection-translation、selection-history、selection-translation-draft 接口；启动时识别中断请求 |
| web/static/reader/ | 手动翻译按钮、原译文侧栏、近期译文、本地缓存标签、用量展示和原文回跳 |
| memory/store.py | 为 AI 草稿保存 generation_source；后续 revision 保留翻译请求 ID 和模型/提示词身份 |

服务继续使用已配置的 TRANSLATION_*，缺省复用 OPENAI_*；新文本模式不依赖 `.translation_env` 或 BabelDOC。没有擅自把文献发送给 Google、百度或其他新服务，也没有读取或修改 Zotero 文献库。没有复制上游 TS 代码、提示词或素材，而是独立实现按需交互；上游为 AGPL-3.0，原整篇翻译后端的许可约束仍按既有文档处理，不能因新增路径而忽略。

## 请求、缓存与来源

请求只有固定的简短 system 翻译指令和当前选区的 user 文本，无聊天历史、标题、全文、格式示例或工具调用。官方 DeepSeek V4 使用显式非思考模式；SDK 重试为 0，单次超时 30 秒，不自动续写、不回退到另一个模型。响应被输出上限截断或没有中文时不缓存为成功。

缓存身份包含原件 SHA256、仅折叠空白后的选区文字、端点哈希、模型、目标语言、提示词哈希、版本和生成设置，不含 API Key。不同模型/版本的结果不互相冒充；正文相同但出现在另一处时复用译文，并使用当前选区的新坐标，不能使用首次翻译的旧位置。

记录保存原件 ID/哈希、页码、多行选区坐标和摘录。译文保存在独立表中，明确标记 machine_translation，不作为独立原文证据，不进入原有 Qdrant 集合。点“保存为待确认草稿”不会调用模型；草稿 author_source=ai_assisted、confirmed=false，并保留生成来源。确认后仍保留 AI 来源和历史，内容不会自动升级为论文事实。

## 用量限制与已知边界

- 一次最多 2400 字符，输出最多 1536 token，一次明确操作最多一次模型请求；全应用同一时间最多一个选区模型请求。并发点击被拒绝，避免双窗口重复付费。
- 默认每日最多 20 个新请求、20000 token 预留/记账额度，按 Asia/Shanghai 日期统计，先在事务中预留，再发送请求。达到任一限制不发新请求，已有缓存仍可用。
- 输入预留采用 UTF-8 字节数加消息开销及输出上限，属于保守估算，**不是服务商精确 tokenizer 或人民币硬预算**。收到有效 usage 后记录输入/输出/缓存/思考及 total；未返回 usage、失败或中断时保留预留，不假装没有费用。
- 以上额度只约束新选区翻译，不覆盖整篇 PDF、问答和其他 Agent 调用。模型单价未核实，没有显示虚假的人民币成本。未来应将各路径统一纳入账本，不能认为全项目现在已有日消费上限。
- 模型返回 usage 但随后输出校验失败，仍保留该 usage；网络异常可能已经在服务商侧计费，本地无法还原，账单以服务商为准。
- 数据库失败或应用关闭造成 pending 时，重启标记 interrupted，不自动重发。只支持单 Web 进程；没有扩展为多 worker 分布式队列。
- 近期译文显示当前论文最近 20 条成功结果；断网仍可打开历史和缓存。清空 SQLite 会同时失去缓存/用量账本，不应以此绕开预算。
- 选区翻译只保证来源可追溯，不能自动证明译文忠实。跨页/跨栏仍需拆开选择，纯扫描图像无法直接划词；保留整篇 PDF 功能的现有限制。

## 已执行验证

- 新增 11 项确定性测试全部通过：只发选区、短请求、禁止未授权外发、缓存跨重建/断网复用、来源坐标、草稿确认与历史、失败不重试、截断仍记录 usage、每日限制、并发重复点击、任务中断及模型身份变化。
- 主环境共 161 项：152 通过、9 项因翻译引擎依赖隔离跳过；这些旧引擎专项上次已在独立环境通过，本轮未改 worker，未重复付费测试。
- 隔离浏览器 8 项检查通过：默认自动整篇关闭、真实 PDF 文字层的程序化多行选区、一次模拟请求、重复命中缓存、AI 来源草稿、未确认不召回、原文选区回跳、刷新后恢复历史。截图 `output/playwright/selection-translation.png` 已查看，无侧栏文字遮挡。该轮并非原生鼠标拖选测试，之前阅读器几何测试保留。
- 浏览器仅使用自建合成 PDF 和模拟 provider，未外发真实文献，也未调用收费模型。测试截图中的中文明确是模拟译文，不包装为学术翻译质量结果。
- 真实 DeepSeek 的新路径译文质量、费用和延迟未测；上次核实余额不足，本轮按节省费用要求不发起真实验证。不能保证达到用户 Zotero 的半年费用。
- 正式部署于 2026-09-10 复验：阅读器 HTTP 200，选区接口可用，自动整篇翻译关闭，选区请求数为 0。原有 2 份原件、0 条笔记及 1 个失败全文任务保持；SQLite 完整性检查通过。配置可用不表示账户余额或模型服务已恢复。8767 模拟服务已关闭，测试数据保留。

复现：

```powershell
D:/miniconda3/envs/deepagents/python.exe -B -m unittest discover -s tests -p test_selection_translation.py -q
D:/miniconda3/envs/deepagents/python.exe -B scripts/selection_preview.py --run zotero01
npx --yes --package @playwright/cli playwright-cli -s=selection open http://127.0.0.1:8767/reader --headed
npx --yes --package @playwright/cli playwright-cli -s=selection snapshot
npx --yes --package @playwright/cli playwright-cli -s=selection run-code --filename scripts/selection_browser_check.js
```

浏览器初次测试请使用全新的 `--run` 名称；第一次请求断言要求无已有译文缓存。重新启动同一名称用于核对持久历史。测试数据只在 output，不应对正式 8765 页面运行测试脚本。测试脚本曾因 CLI 沙箱没有全局 URL 构造器而中止，改为固定本机 URL 前缀检查后通过，未因此调用模型或修改业务数据。

</details>
