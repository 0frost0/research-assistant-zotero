# Go 主后端迁移与验收

更新：2026-11-24。Go 负责知识库工作台、产品 API、SQLite 数据、检索版本和来源范围、任务状态、问答编排、反馈、备份和解析队列。Python worker 提供检索索引、模型调用和 PDF/OCR/MinerU。

## Go 知识库工作台

使用下文的本地 Go 启动入口，在同一端口打开 `/knowledge`（例如 `http://127.0.0.1:8765/knowledge`），输入该引擎的 Zotero 连接令牌。配置了独立模型服务分组时，可用 `#connection=<配置组 ID>` 选择同一分组。独立运行 `researchd` 时，通过 `-web-static research_assistant/web/static` 指定共享工作台资源。

- 浏览器与 Zotero 共用 `/api/knowledge/`、令牌校验和 SQLite 数据，因此笔记修订和备份使用同一写入路径。Go 在检索参数无效时返回 400，索引准备时返回 202，工作台会继续轮询并展示实际检索方式。
- Chrome 隔离验收使用两个合成知识库。断开编码服务后，工作台从索引准备转为关键词检索，仅返回选中库的笔记。点击来源跳到对应笔记，修改后显示 v2。页面下载的 ZIP 用 Python 恢复到独立目录，读回两个库和笔记 v2。验收未调用生成模型。
- 原生 Zotero 10.0.8 首轮能启动 Go，但连接分组尚无服务配置时，worker 在检索前拒绝请求，界面显示 `unavailable`。修复后该分组的检索使用默认配置，模型调用仍要求已选分组有配置。使用 `--native-engine` 在隔离库复验，确认同一端口提供工作台，编码服务不可用时 TF-IDF 命中 1 条笔记，按正文找回目标 revision，退出后 Go 停止。复验仅运行检索，结果保存在本地 `output/zotero/real-go-workbench-native-20261122-r2/result.json`。
- 阅读器、研究项目网页及语义检索索引仍在 Python。此次验收覆盖知识库工作台与本地 API，生产仍使用现有配置。

## 检索版本一致性

- Go 在一次 SQLite 读事务中读取所选知识库的 revision，并将版本表随检索请求传给 Python worker。worker 在准备索引前核对，Go 在交付结果前再次核对 worker 返回值和当前版本。
- 直接检索仍通过 202 提示索引准备。问答和对照在服务端轮询同一次检索时沿用最初版本。资料在这次检索中变化时返回 `revision_conflict`，网页提示刷新知识库后重试。
- Go API 契约测试覆盖检索前、检索中和索引等待时的版本冲突。Python worker 测试核对版本不符时跳过索引，版本一致时返回匹配的 revision。Python 继续负责索引和排序。
- Zotero 10.0.8 隔离库通过 `--native-engine` 复验从插件启动、工作台访问、离线 TF-IDF 检索及引擎停止。自然语言问题命中 1 条笔记，按笔记正文查到目标 revision。实测使用真实库的隔离副本，未调用生成模型。

## 检索来源范围

- Go 在读取知识库 revision 的同一个 SQLite 事务中计算来源名单。PDF 按内容哈希去重，笔记按 ID 和 revision 标识。范围以 Zotero 同步写入的分类成员为准，排除未确认及已归档的个人笔记。指定 `source_ids` 时仅筛 PDF，`both` 仍可返回选中库的笔记。
- 直接检索、问答和文献对照共用这条路径。Python worker 用名单准备索引并保留原有引用元数据与排序。名单为空时跳过索引。Go 对照名单逐条校验命中，拦截范围外结果。独立 Python 工作台继续用自身的来源范围逻辑。
- Zotero 10.0.8 隔离实测覆盖插件启动 Go、访问工作台、编码服务断开时检索、按正文找回目标笔记 revision、退出停止引擎。复现使用下文的 `--native-engine`。来源范围和 worker 契约可用合成测试复现。

  ```powershell
  go -C backend test ./internal/store -run TestScopeForSearch -count=1
  go -C backend test ./internal/api -run TestSearchPinsKnowledgeBaseRevisions -count=1
  python -m unittest tests.test_inference_worker
  ```

## 已验证

- `backend/` 使用 `modernc.org/sqlite`，保留 Python 的知识库身份、同步指纹、不可变 revision 和备份兼容性。
- Go 契约测试覆盖 Python→Go→Python 往返、并发 revision 冲突、同步回滚、任务取消优先于迟到结果、重启中断、重试上限、配置原子更新、附件校验、备份恢复和 worker 断连取消。
- Go 原生 Zotero 隔离验收：`output/zotero/real-questions-go-aligned-20261118/result.json`。五题通过实际工作台提交：四题有回答（EHR、RePrompT、MOTOR、个人记录），火星预算题为 `no_evidence`；10 次模型调用，引用审计 8 条、未匹配 0 条。个人记忆本轮为 `answered`，结果可能随模型调用变化。
- Python 对照验收基线为 `output/zotero/real-questions-python-json-20261118/result.json`：10 次模型调用，12 条引用未匹配 0 条；两轮不是准确率或人工 gold。
- 2026-11-19 使用干净 `6929f3b` EXE 复验：`output/zotero/real-questions-go-release-20261119/`，四题 `answered`、预算题 `no_evidence`，12 次模型调用，8 条引用未匹配 0 条。EHR 本轮触发两轮检索／4 次模型调用，说明固定问题的执行轨迹仍会波动；结果均在原生工作台渲染。
- 运行命令：

  ```powershell
  cd backend
  $env:PYTHON_EXECUTABLE='D:\miniconda3\envs\deepagents\python.exe'
  go test ./...
  go vet ./...
  go build -o ../output/zotero/dev/researchd.exe ./cmd/researchd
  cd ..
  python benchmarks/prepare_real_zotero_run.py --target output/zotero/real-questions-go-new --connection <connection-id> --base <kb-id>
  powershell -File benchmarks/run_real_zotero_questions.ps1 `
    -Target output/zotero/real-questions-go-new -GoBackend `
    -GoExecutable output/zotero/dev/researchd.exe
  ```

## 2026-11-23 降级检索与真实模型验收

- 使用来源范围迁移后的 Go 构建，在 Zotero 10.0.8 隔离工作台完成五题。EHR 定义返回 `partial`，数据集、参数和个人笔记问题返回 `answered`，无关预算问题返回 `no_evidence`。共 13 次真实模型调用，6 条引用全部匹配页面或笔记 revision。结果和引用审计在本地 `output/zotero/real-go-memory-recovery-goapi-20261123/`。
- 本轮远程编码服务连接超时。隔离配置保留生成模型，原文问答使用 `bm25-lexical-fallback`，笔记使用 `tfidf`。多文献对照经原生按钮提交并渲染，但两篇论文都未取得有效候选，返回 `degraded`，生成调用为 0。该结果覆盖降级与空证据处理，在线向量检索和本轮完整模型对照仍待验证。
- 验收 runner 新增 `-GoExecutable`，默认使用 `output/zotero/dev/researchd.exe`，复验历史 EXE 时传入对应路径。每轮使用新的隔离目录。`-GoBackend` 在独立端口启动服务，原生插件启动与退出管理另由 `--native-engine` 覆盖。

## 2026-11-23 在线补验

- 核对当前服务器并恢复编码、重排隧道后，配置中的编码 API 实际返回 2048 维向量，重排请求通过模型版本和分数格式校验。复用上一轮 Go 开发构建，在新的隔离库通过 Zotero 原生工作台提交五题。
- 四题 `answered`，无关预算题 `no_evidence`，共 10 次模型调用。原文使用 `multimodal-hybrid`，原文问答的重排记录均为 `succeeded`，笔记使用 `multimodal`。7 条引用全部匹配页面或笔记版本，五题的候选与引用均通过所选库来源及 revision 核对。本地结果在 `output/zotero/real-go-online-20261123/`。
- 双文献对照从原生勾选和按钮提交，状态 `ok`，4 次模型调用、单轮 AI 复核、6 条比较及 2 个条件摘要，结果在界面渲染。9 段证据全部匹配原页，6 条比较均引用两篇来源。本地结果在 `output/zotero/real-go-online-comparison-20261123/`。本轮使用 `5e211b6` 源码的开发 EXE，构建记录为 `vcs.modified=true`。人工语义正确率和大库性能仍待评估。

## 2026-11-24 较大隔离库检索测量

在 2 个知识库、154 篇去重文献、31,207 个文字切块的隔离副本上测量 Go 搜索 API。首次为新范围准备索引，约 213.9 秒后降级为 BM25；当时只登记完 99 篇索引。续建约 85.1 秒后向量索引可用，但原异常没有保留，根因仍未确认。重启隔离引擎并复用已建索引，首请求（含索引检查、加载、轮询及查询）28.243 秒，随后 14 次请求中位数 310.5 ms、范围 232–361 ms，15 次均为 `multimodal-hybrid`，中文问题回传、来源和 revision 审计全部通过。

这是同机顺序查询的小样本测量，不是优化前后对照或并发性能结论；复用已有 embedding 缓存及本机 Qdrant，未计入回答生成和精排。部分页面文字不足的覆盖告警仍存在。无关问题也可返回候选，候选数量不代表可回答性。此次没有新增拒答阈值或人工语义质量结论，未部署生产。

复现：先按上文准备新的隔离目录，在一个终端运行 `python scripts/start_go_engine.py --root <隔离目录>/backend --port 8790`，就绪后在另一个终端运行：

```powershell
python benchmarks/benchmark_zotero_search_latency.py --target <隔离目录> --all-bases --repeats 3
```

脚本使用 UTF-8 内置问题、与 Go 原文问答一致的文字混合检索参数；逐次验证问题回传、所选库来源及版本。输出仅含计数、耗时、摘要指纹与错误类型；拒绝覆盖旧报告，网络仅访问回环地址且禁止重定向，任何请求失败或降级最终返回非零。首请求与其余请求须分开解释。结果保存到隔离目录；新一轮用 `--output-name <新文件名>`，首次准备与重启复用不能合并成同一延迟指标。

## 明确边界

- 真实库验收使用隔离的 SQLite、对象目录、Zotero profile 和回环服务；没有改写正式 Zotero 资料。
- 当前 Go API 会拒绝尚未移植的视觉证据和 sufficiency 选项，不把它们静默降级成文字回答。
- 2026-11-19 真实 Zotero 对照：`output/zotero/real-comparison-go-release-20261119/`，MOTOR/RePrompT 原生勾选后点击对照，一次请求、4 次模型调用、单轮 AI 复核，状态 `ok`；两篇分析、两个条件摘要、5 条比较，9 段证据全部匹配原页，5 条比较均引用两篇来源。该审计不等于人工语义评估。
- 生产仍使用 `hybrid_rerank`。检索职责完整迁移和网页接口对齐仍待完成。

## 本地 Go 引擎开发入口（2026-11-21）

在仓库根目录构建开发版。下列路径独立于归档的 release EXE。

```powershell
go -C backend build -o ../output/zotero/dev/researchd.exe ./cmd/researchd
```

Zotero 的「本机启动设置」可沿用项目 Python 解释器与工作目录，把启动脚本选为 `scripts/start_go_engine.py`。脚本使用工作目录下的知识库和连接令牌。命令行可用 `--root <隔离数据目录> --port <端口>` 指定测试实例。同一个数据目录仅运行一个引擎。日常 Python 入口仍为 `scripts/start_local_engine.py`。

Python 启动器先在随机回环端口启动私有推理服务，再拉起 Go API。临时 worker 令牌经标准输入管道传给 Go；启动器退出时管道关闭，Go 随之停止。修复了 Go 在监听端口前就打开任务库的顺序问题：重复启动占用同一端口时，已用独立库验证原 running 任务状态得到保留。

**复现**：用含 worker 依赖的 Python 执行上面的 `prepare_real_zotero_run.py` 准备命令，追加 `--native-engine` 并选用新目录，再运行 `run_real_zotero_questions.ps1 -Target <目录>`。桌面 8765 端口需空闲，有效库需含已确认的笔记。此模式通过原生插件保存启动设置、调用启动入口、发起笔记检索，再调用退出时使用的进程停止操作；依赖服务和 SSH 隧道单独准备。

**故障与结果**：首轮把索引准备中当作空结果。补上等待后，第二轮仍收到 `unavailable`；调用栈定位到查询向量请求，本地 30001 无监听。接通既有 Qwen 隧道后，同一 EHR 笔记问题命中 1 条。随后修复仅笔记检索的查询期编码故障，按相同来源和 revision 使用字符 2–4 gram TF-IDF，API 返回实际后端并显示降级提示。第四轮该自然语言问题离线仍为 0 条；第五轮确认相同问题返回 0 条、按笔记正文检索命中原 revision，且停止后 Go API 断开。隔离记录保存在 `output/zotero/real-engine-managed-20261121*`。上述结果只覆盖这份库和这些查询。

第六轮接通 Qwen 复验，API 报告 `multimodal`，自然语言问题命中 1 条，正文查询命中目标 revision，停止后 API 断开。两种连接状态的检索均未调用回答模型。

## 2026-11-21 原生问答历史验收

- Zotero 10.0.8 在隔离真实库中提交“什么是EHR？”，从工作台取消后恢复提问按钮，历史显示“用户已停止”。修复了取消后结果区仍保留检索进度文案的问题。
- 对已有中断任务从历史点击手动重试。验收脚本自动确认一次弹窗，验证新任务关联原任务、重试计数加一、原任务仍为中断。两轮重试均返回 `succeeded / answered`。
- 已评分回答重新打开时原先仍显示评分按钮。插件现从任务详情恢复评分，两轮均通过保存、回看及 API 读回核对。测试评分仅写入隔离副本，无人工质量评价。
- 复现时在上述准备命令追加 `--history`，使用新目录；输入库最近 20 项历史中需有一项可重试的中断任务和一项未评分的已完成回答。Go EXE 为干净 `6929f3b`。第二轮结果在 `output/zotero/real-history-go-20261121-r2/result.json`。
- 引用审计只证明逐字摘录和页面/revision 对得上，不证明语义正确率、充分性准确率或泛化效果。

## 发布与 CI 故障记录

- 提交 `6929f3b` 首次推送两次发生 GitHub 443 TCP 超时；后续 DNS、TLS 和 `ls-remote` 恢复，正常推送成功，远端 SHA 已核对。
- 首轮远端 CI 的 Python、Node、XPI job 成功，Go job 在测试阶段失败。Go job 缺少 Python 环境与存储契约所需的 `pypdf`；提交 `a1f1334` 补显式 Python 3.12、pypdf 与 `PYTHON_EXECUTABLE`，Go 缓存路径改为 `backend/go.sum`。[修复后远端 CI](https://github.com/0frost0/research-assistant-zotero/actions/runs/44342282197) 四个 job 全部成功；本地 Go test/vet 同样通过。旧失败日志下载受连接重置影响，未取得完整 traceback，不将推断写成日志原文。
- `v0.4.0-go-core` 是核心迁移 prerelease。初始 Windows 包由未提交工作树构建，内嵌 `vcs.revision=a4c0c3f`、`vcs.modified=true`，不能以文件名声称它是干净 `6929f3b` 构建。已补发 `-clean.exe`、`-git.zip` 和 provenance JSON，原资产保留；GitHub digest 与本地 SHA256 一致，见 `docs/releases/artifacts/go-core-clean-verification.json`。
- 2026-11-22 已下架两份源码 ZIP，原始产物与构建收据保存在本地。公开源码以重写后的标签为准，EXE 保留其历史构建身份。
- 干净构建已完成：独立 clone checkout `6929f3b`，`go build -trimpath`，`go version -m` 确认完整 revision 与 `vcs.modified=false`。旧源码 ZIP 与 Git blob 仅有 CRLF/LF 差异；新源码包必须使用 `git -c core.autocrlf=false archive`，并逐文件检查，不能假定默认 `git archive` 忽略本机换行配置。
- 后续构建入口：`python scripts/build_go_release.py --checkout <clean-checkout> --output <outside-checkout-new-directory>`。脚本拒绝脏工作树及仓库内输出，核对 EXE revision/modified、源码文件列表和每个 Git blob，输出 SHA256 收据，不自动覆盖旧资产。
- 上述脚本已实际重复构建 `6929f3b`，EXE 与源码 ZIP 的 SHA256 均与已上传干净资产一致（同一 Windows / Go 1.27.3 环境）。
- 原生 runner 现拒绝已有 `result.json` 的目录，避免读取上一轮结果而误报复验成功；每轮准备新隔离目录。新增 `--comparison` 使用真实 MOTOR/RePrompT 文献、原生勾选与对照按钮，一次请求、最多 4 次模型调用，完整回答仅保存在忽略的私有输出目录。
- 工作流验收脚本前两轮失败保留在 `real-workflow-go-release-20261119` 和 `-r2`：首次等待函数把 `ready` Promise 的完成值 `undefined` 当作未就绪；修为检查 Promise 是否存在后，第二次又提前点击刷新前的历史按钮。现改为等待新 revision 文本渲染后再点击。两轮真实上传／同步均成功，不把验收脚本超时混作业务通过。
- 第三轮 `real-workflow-go-release-20261119-r3`：真实 PDF 经 Zotero 导入后成功上传1次、同步文献/笔记2项；原生保存本地笔记、API修订为v2、原生历史显示v1/v2均通过。随后原生精析提交1个任务，状态失败；远端健康检查成功，正在定位实际解析失败原因，尚不能宣称解析全链路通过。验收笔记是隔离流程标记，不作为科研问答或训练数据。
- 解析故障依据：同一图片路径长289字符，普通 `Path.is_file()` 为 false，Win32 `\\?\` 前缀访问为 true；MinerU 已生成文件，读取阶段误判缺失。现对 Windows 缓存目录/图片统一扩展路径，同时保留 resolve 后的目录包含校验；新测试用真实深目录验证可读并拒绝目录外图片，连同现有缓存测试4项通过。该 Python 修复晚于 `6929f3b` release tag。
- Python core/worker/长路径回归共30项完成（1项依赖条件跳过）；CI 增加 worker 和深缓存路径契约。此处不将 Linux CI 当作 Windows 长路径实机验证。
- 第四轮 `real-workflow-go-release-20261119-r4` 已通过：干净 `6929f3b` Go EXE + 修复后 Python worker，真实PDF上传1次、同步2项、笔记v1/v2历史渲染成功，单个 MinerU 任务 `succeeded`、`cached=false`。未调用问答模型；解析使用真实 MinerU。此前失败缓存也已只读恢复为116块、11张图片。未将私有文献、数据库或个人记录上传。
- 验收提交 `43f92cb` 推送两次遇到 GitHub 443 TCP 超时；DNS 能解析但连通性检查失败，稍后正常重试成功，未修改网络/代理全局配置。## 2026-11-26 Text index diagnostics and large-library recheck

- UnifiedMultimodalIndex.sync() now gathers all expected text point IDs and performs one global stale-point reconcile when required. Image cleanup keeps its existing per-source path. A restart with unchanged sources and complete points skips the text collection scroll.
- In-memory diagnostics record phase, source and chunk counts, elapsed time, Qdrant retrieve/reconcile calls, status, and exception type. The knowledge API exposes only these bounded fields; it does not expose paths, endpoints, tokens, or exception text. The latency benchmark records index_diagnostics so index preparation can be separated from query latency.
- On the same isolated copy (2 knowledge bases, 154 deduplicated PDFs, 31,207 text chunks), the historical cold first request was 28,243 ms. The first global-reconcile version measured 24,245 ms; skipping unchanged reconciles measured 20,864 ms; batched text existence checks measured 19,227 ms. Each cold phase has one sample, so these are diagnostic observations, not concurrency or semantic-accuracy claims.
- The latest five queries returned multimodal-hybrid; hot queries were 247--432 ms. UTF-8 roundtrip, source scope, and revision audits passed. Diagnostics showed 153 sources, 31,207 chunks, 122 retrieve calls, and zero reconcile calls, confirming that the unchanged restart path did not scan the full text collection.
- A follow-up isolated run validated a 1,024-ID Qdrant retrieve batch (memory Qdrant returned all IDs). The real isolated run used 31 retrieve calls and 5,718 ms for sync; its cold first request was 21,121 ms. This is one sample and is recorded as a diagnostic observation, not a stable total-latency claim. The batch is bounded to 256--1,024 and can be set with `RESEARCH_ASSISTANT_QDRANT_RETRIEVE_BATCH_SIZE`.
- Failure boundaries remain explicit: missing Qdrant points trigger re-indexing; source replacement or deletion triggers cleanup; image/text shared-collection behavior is unchanged. No refusal threshold, training label, or production deployment was added.

Reproduce with a fresh output filename:

    python scripts/start_go_engine.py --root <isolated-dir>/backend --port 8790
    python benchmarks/benchmark_zotero_search_latency.py --target <isolated-dir> --all-bases --repeats 1 --output-name search-latency-after-reconcile.json

Detailed local evidence is in output/task-reports/2026-11-26-index-reconcile-diagnostics.md; isolated receipts are search-latency-after-reconcile-r4.json and search-latency-after-reconcile-r5.json.
