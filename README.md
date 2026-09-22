# 科研文献与计划助手

当前部署（2026-09-20）：项目运行于 `agent-assistant:/srv/research-assistant/app`，网页、Qdrant、Qwen 嵌入和 MinerU 均由远端 user systemd 服务管理，只监听服务器回环地址。本机运行 `scripts/start_agent_assistant_tunnel.ps1`，再访问 [阅读器](http://127.0.0.1:8765/reader)。本机项目和旧服务器均保留为回退副本；本次只从旧服务器复制了已授权的 Qwen3-VL-Embedding-2B 与 MinerU2.5-Pro-2605-1.2B，没有从公网下载模型权重。部署细节和验证证据见 [迁移报告](docs/deployment/agent-assistant迁移报告.md)。

模型 API 在服务器 `/srv/research-assistant/app/.env` 修改，文件权限为 `600`。问答与 Agent 使用 `OPENAI_API_KEY`、`OPENAI_MODEL`、`OPENAI_BASE_URL`；全文/选区翻译默认复用这三项，也可用 `TRANSLATION_API_KEY`、`TRANSLATION_MODEL`、`TRANSLATION_BASE_URL` 单独覆盖。修改后执行 `ssh agent-assistant "systemctl --user restart research-assistant-web"`，无需重启 Qdrant、MinerU 或嵌入服务。不要把密钥写入 README 或提交到仓库。

## 中文阅读、划线与长期笔记

“选区联动”：在中文译文中选词句，本地双语模型寻找对应原句及词语，再用原文自身文字坐标高亮（紫色，待核对）。用户已确认的相同选区关联优先显示蓝色。歧义时拒绝匹配，不用版式或字数比例扩大选区，不自动写入笔记；无付费 API 调用。能力边界与真实结果见 [自动词句对齐](docs/reading/自动词句对齐.md)。

纯编号参考文献页保留原件英文及版式，不要求翻译成中文；正文与混合页仍检查漏译。完整性校验失败但引擎已生成 PDF 时，重试会优先本地修复和重新校验，不直接整篇重译。检测不确定时保持严格检查。

点击“翻译全文”后在页面弹窗确认；下方立即显示提交状态，随后显示当前阶段、真实百分比（引擎提供时）和已运行时间，每两秒更新。同篇旧失败任务会重新排队，已有成功译文直接复用；状态查询断网时保留上次显示并提示。

阅读器已增强 PDF 清晰度，提供“150%阅读”。中文选区加入笔记来源后，可“补选原文对应段落”→选中原文→确认→保存；以后点击“返回对应原文”。这是用户确认的关联，未关联时仅定位译文，不声称自动对齐。

**阅读目标：整篇中文 PDF + 可追溯笔记。** 选区翻译仅作补充；全文费用与质量仍需改进，最新结论见 [Zotero 全文翻译调研](docs/reading/Zotero全文翻译调研.md)。

新版已加入：**中英同步翻页、适应宽度、专注阅读、笔记分栏标签、全文用量统计及循环保护**。全文翻译不设单篇或每日 token/请求总数上限，展示本次输入、输出及跨重试累计用量，未知用量单列。保留有限重试、异常重复请求、超时及手动停止保护。真实译文质量与费用仍待验证，见 [简版交付记录](docs/reading/全文翻译与阅读器更新.md)。

入口：[阅读与记忆](http://127.0.0.1:8765/reader)。主页的 PDF 和证据卡片也可进入。原文、中文译文、个人记录和未确认 AI 草稿分开保存；译文未自动对齐原文，支持独立双语对照与手动补选来源。

服务器已经安装阅读器依赖，无需日常重复执行安装脚本。`scripts/install_reader.ps1` 和 `start_web.ps1` 仅供 Windows 本地开发/回退，不是当前服务器入口。配置示例见 [.env.reading.example](.env.reading.example)；`RESEARCH_TRANSLATION_ENABLED=false` 可关闭翻译服务。阅读器内的自动开关控制英文新上传是否进入整篇翻译队列，**默认关闭**。开启后会向配置的模型发送论文文字并可能产生费用。

日常阅读推荐：原文选一句／一段 → **翻译选区** → 侧栏查看机器译文 → 可保存为待确认 AI 草稿 → 编辑确认后进入笔记检索。新文本模式不需要 BabelDOC 或独立翻译环境；原文版式保持不变，中文显示在侧栏，不生成中文 PDF。成功结果保存到 SQLite，同一原件、文本和模型配置重复翻译命中缓存，不再请求模型；近期译文可返回原文选区。

选区模式每次最多 2400 字符、一次模型请求、30 秒超时，不自动重试；每日最多 20 个新请求及 20000 token 预留/记账额度。这是选区路径的本地限制，不是全项目人民币预算。未返回用量的失败保留预留，整篇翻译仍为独立的较高消耗选项。源码依据、实现和模拟验证见 [Zotero 按需翻译借鉴报告](docs/reading/Zotero按需翻译借鉴与实现.md)。

无需模型即可阅读原件、划线、保存/修改/归档笔记、查历史和检索自己的记录。写笔记先选择文字或从证据卡片带入出处。每次编辑追加 revision；多窗口冲突保留输入。中文译文可下载，旧译文与原件按哈希永久保留，删除当前检索资料不会删除阅读归档。

翻译当前限定有文字层英文 PDF；扫描/低文字页保守拦截，复杂公式表格需核对。进度来自真实引擎；成功仅表示自动产物检查通过，不能替代翻译质量验收。实际缺陷、数据库迁移、来源规则、测试结果和五分钟演示见 [完整设计与验收报告](docs/reading/设计与验收.md)。[引擎选择与 AGPL 影响](docs/reading/翻译引擎与许可证.md)必须在发布或公开部署前阅读；本轮未擅自重新许可整个仓库。

```powershell
& D:\miniconda3\envs\deepagents\python.exe -B -m unittest discover -s tests -q
& D:\miniconda3\envs\deepagents\python.exe -B scripts/evaluate_reading_notes.py
# 以下会真实调用已配置模型，只用自建合成样本：
& .\.translation_env\Scripts\python.exe scripts/reading_sample.py --translate
& D:\miniconda3\envs\deepagents\python.exe -B scripts/reading_live_check.py
# 隔离浏览器验收：8766，测试笔记放在 output/，不进入正式数据库
& D:\miniconda3\envs\deepagents\python.exe -B scripts/reading_preview.py
```

实际中文找回小样本：关键词 4/4、模糊问法 2/4、无答案未召回 4/4；不能当作整体准确率。真实合成双栏翻译与真实 AI 草稿/笔记问答已经验证。用户授权后，MDAgent 前两页已通过 DeepSeek 真实翻译，但摘要漏译、作者及脚注排版异常，**质量验收不通过**；已补充长段未译原文拦截。详见 [真实论文两页验收报告](docs/reading/MDAgent两页翻译验收.md)，不得把该节选当作完整论文译文。

翻译等待/失败修复：DeepSeek 在启动排版前检查余额；余额不足、凭据错误不会反复请求。DeepSeek V4 显式使用非思考模式，最多 4 个并发、QPS 2；网络/限流/临时服务错误最多 3 次尝试。旧失败任务点击“重试”会按当前配置建立新身份，保留旧数据。本次真实复验确认账户余额不可用，尚未测得提速或合格新译文；见 [故障原因、修复与验证边界](docs/reading/翻译故障与性能修复.md)。充值或恢复有效凭据后再手动重试，程序不会自动充值或重跑。

首次阅读建议先看 [目录与模块导览](docs/目录与模块导览.md)，按“网页 → 检索 → Agent”的路径理解代码。业务代码集中在 `research_assistant/`，回归测试在 `tests/`，文档在 `docs/`；根目录的三个 Python 文件只保留启动入口。

完整架构与端到端原理先看 [项目介绍](docs/项目介绍.md)；实现细节、实验和取舍见 [项目技术报告](docs/项目技术报告.md)。当前专注文献问答与多文献对照，研究计划暂缓重构。

面向求职的学习方式见 [岗位对齐与学习路线](docs/岗位对齐与学习路线.md)。当前课程是 [Agent 可观测性与并行实验](docs/学习笔记/01_Agent可观测性与并行实验.md)：包含为什么做、关键代码、开源源码参考、实验结果及用户自己的小改动；项目完成和个人掌握分别验收。

当前支持：

- 逐页读取 PDF、Markdown、TXT；低文字 PDF 页按需 OCR
- 可选调用远程 MinerU 精析 PDF，并持久化表格、图片说明、公式和页面坐标
- Qdrant 持久文字/视觉索引，以及多模态、TF-IDF、BGE、混合检索
- 资料选择、文件类型筛选、最低相关度和结果数量控制
- 在网页预览检索词、文件、页码、分数和原文片段
- 基于检索证据回答问题并给出来源和页码
- 多文献对照：按篇取证、并行 Agent 分析、综合比较、单轮 AI 复核及本地历史
- 对照执行记录：阶段/角色计时、排队与请求耗时、token 用量覆盖和错误分类；串行/并行实验工具
- 根据文献证据生成结构化研究计划
- 固定检索一次并生成答案；结构化解析失败时降级为普通文本
- 可选保存计划 JSON
- 多模态检索开发评测集、Recall@K / MRR / 页码召回与人工相关性审阅
- MinerU 表格单元格存储、分页关键词检索及受限只读 SQL
- 研究项目工作台：项目记忆、任务进度、LangGraph 持久化规划、规则检查与人工审批

阅读模块验收：本轮同步阅读浏览器 13 项通过，3 项选区几何检查通过，独立引擎模拟 11 项通过；Python 全套最新计数见 [验证记录](benchmarks/reading/verified_results.json)。全部新验收使用隔离数据，未向正式库写入测试笔记。本轮真实模型测试未执行完成，不用模拟结果代替质量或费用数据。

## 多文献对照

新运行结果中可以展开“执行记录”，查看取证、分析、综合和复核的时间与调用状态。只记录执行元数据，不额外保存原始提示词或响应全文；未知 token 不当作 0，部分报告不当作完整费用。历史运行没有轨迹时如实显示缺失，不推算补填。普通问答路径暂未接入此轨迹模块。

模拟并发实验不使用真实文献、不调用外部模型；输出文件必须使用新名字，防止覆盖旧实验：

    D:\miniconda3\envs\deepagents\python.exe -B -X utf8 benchmarks/run_comparison_experiment.py --mode simulated --repeats 3 --output benchmarks/my_concurrency_experiment.json

实验只测固定证据后的生成编排，不计入 PDF 处理、嵌入或检索。`--mode live` 必须另外提供符合脚本 schema 的证据快照 `--dataset`、`--allow-external` 和足够的 `--max-model-calls`，没有这些不会启动真实调用。真实重复实验可能收费，不要为复现模拟实验打开这些开关。

当前开发重点是文献问答。研究计划和 `/projects` 保留现状，后续再重构。

在首页切换“多文献对照”，从资料库选择 **2–4 篇**文献，提出具体比较问题，例如“这些研究的方法、评价条件和结论有哪些共识与差异，哪些结果不能直接比较？”。每篇取证 1–8 条，默认 5 条；确认外发许可后开始。网页显示阶段、已分析篇数和模型调用数，可停止、刷新并查看最近 20 次对照记录。

流程是 `按篇取证 -> 并行单篇分析 -> 综合对照 -> 一轮复核 -> 结果`。不同 Agent 可以使用同一个 DeepSeek 模型，但职责、可见证据和输出 schema 分开。最多 4 个单篇分析并行，生成模型调用最多为文献数 + 2（最多 6 次），不自动重试、不递归讨论、不再次调用模型修稿。复核不支持或不确定的比较放到“未采纳 / 待核实”，不是悄悄隐藏失败。

程序校验引用编号必须属于本轮真实取证，单篇分析不能串用别篇，比较必须引用至少两篇。点击引用可展开保存的原始证据片段及页码。**AI 复核不等于人工核验，检索片段分析不等于全文精读。** 本流程不向回答模型发送图片像素，也不把图表转写当成已验证事实；精确表格数值仍需核对原表。单篇草稿与已复核的比较分别展示。

生成调用外层超时 35 秒（现有模型连接配置可能更短），取证等待 45 秒，整轮 180 秒；每次最多输出 2600 token。单篇失败时其他篇继续；不足两篇有效分析时只返回单篇草稿和证据缺口。同步索引读取不能强行杀线程，取消或超时后会阻止后续模型调用，并阻止堆积新的取证线程。已经发送的模型请求仍可能计费。

`.data/comparisons.sqlite3` 保存问题、选用资料与解析版本摘要、本轮证据快照、单篇分析、复核和最终结果，含文献内容，勿公开上传。它不是向量库，也不是 LangGraph checkpoint。服务重启后未完成任务标记“中断”，不会自动重发付费请求；查看历史不会再次调用模型。当前是本机单用户、单服务进程设计，一次仅执行一个对照任务。原有问答、研究计划、评测标注和向量索引未改动。

实现与原理详见 [技术报告第 22 节](docs/项目技术报告.md#22-多文献对照与有界多-agent)。专项测试：

    D:\miniconda3\envs\deepagents\python.exe -B -X utf8 -m unittest -v tests.test_comparison

`benchmarks/serve_comparison_fixture.py --port 8766` 是使用临时数据和模拟 Agent 的浏览器验证服务，不是正式科研服务。本阶段离线流程已验证，尚未用真实 DeepSeek 输出验证科研比较质量。

## 研究项目工作台

入口：[研究项目](http://127.0.0.1:8765/projects)。原有文献问答和一次性计划不变，新工作台用于持续跟进一个研究项目。

1. 新建项目，在“项目记忆”保存研究目标、设备/数据约束、工时预算和选用资料。
2. 在“任务进度”手动添加任务，或在“规划与审批”要求助手拟定新增任务。生成规划前需单独允许外发项目数据。
3. 草案通过工时、重复任务和依赖检查后暂停。只有点击“批准并添加任务”才写入任务清单；拒绝不修改任务。
4. 更新任务状态和进展备注，后续规划会读取这些记录。依赖任务未完成时，不能将下游任务设为进行中或已完成。

这里的工时预算约束所有未完成任务，不是按日历自动滚动的一周排程。完成任务后释放对应预算；修改预算不会擅自删除旧任务。项目可归档并恢复，不提供永久删除。

规划工作流为 `draft -> check -> approval -> apply`，使用真实 LangGraph `interrupt()` / `Command(resume=...)` 和 SQLite checkpoint。刷新网页不丢草案；服务中途停止后显示“恢复执行”，不会自动重发模型请求。恢复前需再次确认可能外发的数据；已有草案会复用，审批恢复不重新拟定。

单次规划最多检索一次、调用一次 DeepSeek（输出上限 3500 token），规则检查不额外调用模型。未选择资料时仅按项目目标和进展规划，不声称有文献支持。它只建议并管理任务，**不会实际运行实验、上传数据、改论文或执行服务器命令**。模型格式错误或连接失败时显示失败记录，可重试或放弃；不会把不完整输出直接应用。

本地持久化：`.data/projects.sqlite3` 保存项目、任务、草案与活动记录，`.data/project_checkpoints.sqlite3` 保存执行 checkpoint。二者不等于向量库，均受 `.gitignore` 排除。它们含项目内容，备份时应一起保存且妥善保护；不要把 API Key 写入项目记忆。已有 `plans/*.json` 不会自动导入或改写。

当前环境已有 `langgraph 1.2.11`、`langgraph-checkpoint 4.2.0`、`langgraph-checkpoint-sqlite 3.1.1`。模型继续使用现有 `.env` 配置。本阶段未安装新包、未改远程服务或向量模型。

实现细节和 Agent 知识对应关系见 [技术报告第 21 节](docs/项目技术报告.md#21-研究项目工作台与持久化规划)。专项离线测试：

    D:\miniconda3\envs\deepagents\python.exe -B -X utf8 -m unittest -v tests.test_project_workflow

浏览器隔离验证入口由 `benchmarks/serve_project_fixture.py --port 8766` 启动：使用临时数据库和模拟规划器，不调用 DeepSeek；关闭后清理临时数据。它不是正式助手服务。

## 文献问答

    D:\miniconda3\envs\deepagents\python.exe D:\workspace\research_assistant\app.py --topic "4DCT 重建" --question "这些资料中的系统主要包含哪些模块？"

## 生成研究计划

    D:\miniconda3\envs\deepagents\python.exe D:\workspace\research_assistant\app.py --topic "4DCT 重建" --hours 12 --question "根据资料为我制定本周的原型验证计划" --save-plan

## 启动网页

    .\scripts\start_agent_assistant_tunnel.ps1

浏览器打开 http://127.0.0.1:8765。脚本建立 `127.0.0.1:8765 -> agent-assistant:127.0.0.1:8765` 的 SSH 隧道；远端服务不暴露公网端口。

隧道 PID 保存在 `.agent_assistant_tunnel_8765.pid`。需要重连时，只结束该文件记录且命令行确认属于本项目的 `ssh.exe`，再重新运行脚本；不要用 `stop_web.ps1` 停止远端服务。

网页提示无法连接时，先重新运行隧道脚本。若仍失败，再执行 `ssh agent-assistant "systemctl --user is-active research-assistant-web research-assistant-qdrant research-assistant-embedding research-assistant-mineru"` 检查远端四项服务。

不传 --library 时，程序读取当前项目的 library 目录。

## 本地测试

不调用外部模型：

    D:\miniconda3\envs\deepagents\python.exe -B -X utf8 -m unittest discover -s tests -t . -p "test*.py"

2026-09-09 模块化后全套 113 项通过。启动器通过 `/api/health` 检查本地网页存活；`/api/status` 才检查远程嵌入等依赖，所以网页可打开不等于远程模型已就绪。

运行固定检索评测：

    D:\miniconda3\envs\deepagents\python.exe evaluate.py --backend tfidf

评测用例位于 `benchmarks/datasets/eval_cases.json`。只有同时传入 `--with-agent --allow-external` 才会把评测命中的片段发送给外部模型：

    D:\miniconda3\envs\deepagents\python.exe evaluate.py --with-agent --allow-external

网页默认使用统一多模态检索：文字、图片和问题都由 `Qwen/Qwen3-VL-Embedding-2B` 编码为 2048 维向量，在同一个 Qdrant collection 中执行一次搜索。图片与 MinerU 原有图注作为联合输入，一次得到一个向量，不调用第二个模型生成描述。普通问题用科研检索指令，明确找图的问题用图像检索指令，资料用默认表征指令；这不是不同模型，也不增加搜索次数。TF-IDF、BGE、混合检索和纯文字 Qdrant 仍作为可选基线。

## 文本解析与向量索引的关系

MinerU、`pypdf` 和 Tesseract 只负责把文件变成可检索内容块，不负责生成向量。下一次检索时，应用按网页选择建立或更新索引：

- TF-IDF：资料库用 `fit_transform()` 建立词表和文档矩阵，问题只用同一个向量器的 `transform()`，两边共享完全相同的词表和 IDF。
- BGE：资料和问题都使用本地 `BAAI/bge-small-zh-v1.5`；`embed_documents()` 与 `embed_query()` 最终调用同一个 `_embed()`，使用相同 tokenizer、模型、CLS pooling 和 L2 归一化。
- hybrid：分别计算同空间的 TF-IDF 和 BGE 相似度，再按 `0.6 / 0.4` 合并分数。
- Qdrant：资料块和问题都使用同一个本地 BGE 实例。SQLite 保存证据正文、页码、bbox、图片路径、内容哈希和激活版本；Qdrant 保存向量和可过滤 payload。查询命中后还会回 SQLite 校验当前激活版本。
- multimodal：文档文字、图片像素与图注、用户问题全部使用同一个 Qwen3-VL-Embedding-2B。文字点与图片点写入同一个 collection；问题只编码一次、Qdrant 只搜索一次，不进行 BGE/CLIP 分数或排名融合。共享空间不保证文字和图片分数完全校准，质量需要单独评测。

Qdrant 后端按每份文件的解析内容、切块规则、模型名称和向量维度计算版本哈希。内容未变化时跳过向量化；更新时先暂存 SQLite 证据并写完全部新向量，再原子切换激活版本，最后删除旧向量。Qdrant 不可用时会自动降级到同一 BGE 模型的内存检索。

## PDF 提取

网页资料库中的“检查 PDF 提取”会显示每份 PDF 当前使用的解析器。系统默认使用快速解析；点击 PDF 右侧的“精析”后，后台通过 SSH 隧道连接远程 MinerU。完成结果缓存在本机，后续重建索引时自动优先使用，不会在每次提问时重复解析。

MinerU 使用前先启动本地隧道：

    .\scripts\start_mineru_tunnel.ps1

确认 `http://127.0.0.1:30000/health` 可访问后，在网页点击“精析”。默认配置为：

    $env:RESEARCH_ASSISTANT_MINERU_URL = "http://127.0.0.1:30000"
    $env:RESEARCH_ASSISTANT_MINERU_TIMEOUT = "1800"

缓存位于 `.cache/mineru`，以 PDF 内容哈希区分版本。删除资料库 PDF 时，对应缓存会同步删除。远程服务不可用或尚未精析的 PDF 仍使用 `pypdf + Tesseract` 快速路径。

本机当前只有英文 OCR 语言包。处理中文扫描件前需安装 Tesseract `chi_sim`，并配置：

    $env:RESEARCH_ASSISTANT_OCR_LANG = "chi_sim+eng"

可用以下环境变量关闭 OCR 或修改模型请求超时：

    $env:RESEARCH_ASSISTANT_ENABLE_OCR = "false"
    $env:RESEARCH_ASSISTANT_MODEL_TIMEOUT = "30"

快速路径只覆盖原生文字和扫描页文字，图片页统计不能视为已经理解图片内容。MinerU 路径会保存内容类型、页码、边界框、表格 HTML、图注及可用的图片/图表说明，但识别结果仍需在重要引用处人工核对原 PDF。

当前解析器与 MinerU/Docling 的第一轮实验、性能数据和已知限制记录在 `benchmarks/PDF解析器对比报告.md`。

LangSmith Trace 默认关闭，以免网络受限时影响主流程。如需启用：

    $env:RESEARCH_ASSISTANT_ENABLE_LANGSMITH = "true"
    D:\miniconda3\envs\deepagents\python.exe D:\workspace\research_assistant\web_app.py

DeepSeek 连接失败不影响“只检索”；默认 Qwen 检索仍需要连接你指定的远程 GPU 服务器。完全断网时可使用 TF-IDF 或已缓存的本地 BGE。

## Qdrant Server

本机已使用 Docker Desktop 运行 Qdrant 1.19.1。Qdrant 负责持久化保存和检索向量，不负责解析 PDF，也不会自行生成向量。当前服务配置为：

    REST / Dashboard: http://127.0.0.1:6333
    gRPC:              127.0.0.1:6334
    容器名:            qdrant-research-assistant
    数据卷:            research-assistant-qdrant-data

端口只绑定本机回环地址，不对局域网开放。容器使用 `unless-stopped` 自动重启策略；Docker Desktop 启动后，Qdrant 会自动恢复。

常用命令：

    docker ps --filter name=qdrant-research-assistant
    docker logs qdrant-research-assistant
    docker restart qdrant-research-assistant
    Invoke-RestMethod http://127.0.0.1:6333/healthz

可重复部署配置见 `deployment/qdrant/compose.qdrant.yaml`。现有数据位于声明为 external 的 Docker 命名卷中，停止或重建容器不会删除该卷；Compose 也不会替你删除这个数据库卷。

Python 客户端安装在 `deepagents` 环境中；新环境需要执行：

    D:\miniconda3\envs\deepagents\python.exe -m pip install qdrant-client

应用默认配置如下，可在 `.env` 覆盖：

    RESEARCH_ASSISTANT_QDRANT_URL=http://127.0.0.1:6333
    RESEARCH_ASSISTANT_QDRANT_COLLECTION=research_assistant_text_v1
    RESEARCH_ASSISTANT_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
    RESEARCH_ASSISTANT_MULTIMODAL_COLLECTION=research_assistant_multimodal_v1
    RESEARCH_ASSISTANT_EMBEDDING_URL=http://127.0.0.1:30001
    RESEARCH_ASSISTANT_QDRANT_BATCH_SIZE=96

实际 collection 名自动追加维度和模型配置哈希，Qwen 使用 2048 维的新 collection。指纹包含权重、关键预处理文件、官方 helper、推理配置及查询任务指令；旧 ChineseCLIP/BGE collection 保留但不参与默认检索。Evidence Store 位于 `.data/evidence.sqlite3`。当前 collection 与实测详情见 `benchmarks/unified_qwen_results.json`。

Qwen 权重保存在远程项目 `models/Qwen3-VL-Embedding-2B`。服务通过 GPU UUID 绑定物理 GPU 2，远程仅监听 `127.0.0.1:30001`；MinerU 继续使用物理 GPU 1/30000。混合显卡机器的 CUDA 整数编号不一定等于 nvidia-smi 编号，因此不能直接照抄 `CUDA_VISIBLE_DEVICES=2`。新增依赖仅安装到 `.embedding_deps`，未升级 MinerU 的已有依赖。

远程启动服务（服务器重启后需要重新启动，当前不是 systemd 服务）：

    ssh -p 36969 dingone@10.10.129.37 /home/dingone/dst/research_assistant/.venv/bin/python /home/dingone/dst/research_assistant/start_remote_embedding.py

本机启动独立隧道，再启动网页：

    .\scripts\start_embedding_tunnel.ps1
    .\start_web.ps1

健康接口为 `http://127.0.0.1:30001/health`。嵌入请求通过 SSH 传输文字或图片字节，不接受公网图片 URL，不在远程服务日志记录请求正文。远程同机用户仍属于信任边界；服务不是可直接公网部署的鉴权 API。

当前仍保留 450 字符、重叠 80 字符的向量切块基线。统一检索命中后按出处补回同一原始块，单条上下文最多 1800 字符，不重新编码。Qwen 服务配置长度上限 8192 token、图片最大约 104 万像素、每批最多 4 项；长文本和精细图表仍需进一步优化。首次索引构建发生在检索初始化中，后续复用持久化向量。最终 DeepSeek 不直接看图；默认统一路径只传图像定位信息和风险提示，不把未核验图表转写当作可引用正文。

重跑真实检索诊断（只连接用户指定服务器，不调用 DeepSeek）：

    D:\miniconda3\envs\deepagents\python.exe -X utf8 benchmarks/evaluate_unified.py --full

## 评测与结构化表格

网页入口：[评测与表格](http://127.0.0.1:8765/lab)，也可从问答页右上角进入。

- 评测：当前有 18 道开发正例（文字/图片/混合证据各 6）、2 道无答案对照、4 道图像输入自匹配检查。标准答案是助手草拟的开发标注，不是人工金标准。`query_type` 表示期望证据类别，`input_modality` 单独表示输入是否含图片。
- 在“原始向量排名”和“网页显示排名”之间切换，可查看 Recall@1/3/5/10、MRR@10、页码召回。人工审阅是可选的质量核验：仅需对抽样或关键题核对原文，主动确认期望证据、填写审阅人并给结果打 0~3 分。日常提问无需填写这些表单；未评分不记为 0 分。
- 表格：按来源/关键词检索、分页浏览、核对原 PDF 和截图。当前 125 张表、2890 个锚点单元格；113 张结构可解析，12 张缺少 HTML，不能靠截图伪造单元格。
- SQL：只允许读取 `research_tables`、`table_cells` 的受限 SELECT/WITH；最多返回 200 行，超时约 2 秒。选择表格后，可载入“当前表单元格”查询。`row_index`/`column_index` 从 0 开始，页码从 1 开始。含单位、百分号、千分位的数值仍保留原文，不擅自转换。

表格在访问表格 API 时同步现有 MinerU 缓存；未变化的来源会跳过。它不重新 OCR、不调用 Qwen，也不重建向量库。目前表格查询是独立的网页检索/SQL 路径，**没有自动接入 DeepSeek 的 SQL Agent**。

评测数据：`benchmarks/multimodal_eval_v1.json`；不可变运行快照与人工评分：`.data/evaluations.sqlite3`；结构化表格：`.data/tables.sqlite3`。文献内容和版本改变时旧评测集会拒绝运行。修改标准答案必须建立新版本并重新确认，不能用新资料冒充旧评测。

在项目目录执行真实评测（只使用指定远程 Qwen 服务和本机 Qdrant，不调用 DeepSeek）：

    D:\miniconda3\envs\deepagents\python.exe -X utf8 benchmarks/run_retrieval_evaluation.py

如需基于已审核的源块生成另一个数据集文件，使用 `benchmarks/build_multimodal_dataset.py --output <新文件>`。脚本拒绝覆盖已有数据集；选定新数据集可通过 CLI 的 `--dataset <文件>` 运行。网页当前固定使用 v1。

首轮历史策略的原始 Recall@10 为 83.3%、显示为 47.2%。最新 `research-context-v2` 在同批候选对照下，显示证据 ID Recall@10 为 83.3%，补全后的上下文召回为 88.9%；混合证据两项均为 66.7%。这是 18 道固定开发题的结果，不是回答正确率，也不是独立测试集验收。完整口径见 [多模态检索评测报告](benchmarks/多模态检索评测报告.md)。

## 日常使用与自动回归

日常照常提问即可，标注不在问答必经流程中。开发阶段复用固定评测集自动比较检索结果，人工只用于抽样核对、重要科研结论以及新评测集的最终确认；AI 辅助评分始终单独记录，不冒充人工批准。

本轮统一多模态路径的改进：

- 移除每份文献最多 3 条的硬限制，过滤明确标注的页眉、页脚和页码。
- 通过来源、页码、bbox 和精确片段定位原始块，补全同块上下文并合并重复内容；匹配不唯一时保留原片段，不猜出处，不跨页拼接。
- 图像仍展示并保留原 PDF 入口；未核验转写折叠保存，不进入默认回答证据正文。表格数值与单位保留未经核验提示。这不等于修正了 MinerU 输出或已有向量。
- `/lab` 新运行显示旧策略/当前证据 ID/当前上下文三项对照。旧运行和旧 AI 标注保持原样；新上下文不会继承旧分数。

在项目目录一键执行离线测试及固定检索回归：

    .\scripts\check_retrieval.ps1

需要 `deepagents` 环境、本机 Qdrant 和已连通的远程 Qwen 服务。脚本先运行全部离线测试，再检索 24 个固定案例并保存新运行；不调用 DeepSeek、不自动评分、不定时运行，也不在每次提问时执行。若某道开发题的当前上下文 Recall@10 低于同批候选旧策略的证据 ID Recall@10，结果仍保存但退出码为 2；其他运行失败返回非零。证据 ID 的退步另行记录，这个门槛不是科研质量达标判据。

网页日常默认 5 条证据，评测对照使用 10 条，不能混用：本轮展示 ID Recall@5 为 66.7%，上下文 Recall@5 为 72.2%。两个无答案对照仍返回证据，复杂图文问题仍有缺失；关键图表和数值需要核对原文。以上选择与补全策略仅接入默认 `multimodal`，TF-IDF/BGE/hybrid/纯文字 Qdrant 基线未同步改动。

## AI 辅助标注

已对 2026-09-06 10:58 UTC 的运行完成 24 题、326 个问题-证据组合的 AI 评分。在 `/lab` 选择该历史运行，可查看独立 AI 均分、相关率、逐条理由和 12 道重点复核题。人工表单不会自动填入 AI 分数，人工确认数仍为 0；原有 Recall/MRR 与 v1 标准证据未改动。

结果保存在评测库的独立 `ai_reviews` 表，显式决策文件为 `benchmarks/ai_review_723ea83a.json`。完整方法、统计、图表转写风险与证据遗漏分析见 [AI 辅助标注报告](benchmarks/AI辅助标注报告.md)。这不是独立人工金标准，不能据此宣称模型已满足科研检索要求。

只读检查标注完整性：

    D:\miniconda3\envs\deepagents\python.exe -B -X utf8 benchmarks/review_evidence.py --validate benchmarks/ai_review_723ea83a.json

已审核的包可通过同一脚本的 `--compile` 参数导入 AI 专表并导出规范 ID。相同包幂等，不同内容拒绝覆盖；不会跨运行复制标注。回归：`python -B -m unittest tests.test_core tests.test_retrieval_lab tests.test_ai_review`。

## 项目技术报告

完整的系统目标、架构、数据流、存储结构、检索算法、Agent 工作方式、可靠性设计、实测结果和已知限制见 [项目技术报告](docs/项目技术报告.md)。

## 开发与排错笔记

项目开发和使用过程中遇到的问题统一记录在 [问题排查笔记](问题排查笔记.md)。每次诊断或修复后，都要补充现象、根因、处理方式和验证结果；笔记中不得记录 API Key 或用户文献原文。
