# Go 主后端迁移与验收

更新：2026-11-19。Go 负责产品 API、SQLite 数据、revision、任务状态、问答编排、反馈、备份和解析队列；Python 保留模型调用、PDF/OCR/MinerU 以及当前检索索引 worker。当前不能称为“Python 仅模型/OCR”，因为检索仍由 worker 提供。

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
  go build -o ../output/zotero/researchd.exe ./cmd/researchd
  cd ..
  python benchmarks/prepare_real_zotero_run.py --target output/zotero/real-questions-go-new --connection <connection-id> --base <kb-id>
  powershell -File benchmarks/run_real_zotero_questions.ps1 `
    -Target output/zotero/real-questions-go-new -GoBackend
  ```

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
- 干净构建已完成：独立 clone checkout `6929f3b`，`go build -trimpath`，`go version -m` 确认完整 revision 与 `vcs.modified=false`。旧源码 ZIP 与 Git blob 仅有 CRLF/LF 差异；新源码包必须使用 `git -c core.autocrlf=false archive`，并逐文件检查，不能假定默认 `git archive` 忽略本机换行配置。
- 后续构建入口：`python scripts/build_go_release.py --checkout <clean-checkout> --output <outside-checkout-new-directory>`。脚本拒绝脏工作树及仓库内输出，核对 EXE revision/modified、源码文件列表和每个 Git blob，输出 SHA256 收据，不自动覆盖旧资产。
- 上述脚本已实际重复构建 `6929f3b`，EXE 与源码 ZIP 的 SHA256 均与已上传干净资产一致（同一 Windows / Go 1.27.3 环境）。
- 原生 runner 现拒绝已有 `result.json` 的目录，避免读取上一轮结果而误报复验成功；每轮准备新隔离目录。新增 `--comparison` 使用真实 MOTOR/RePrompT 文献、原生勾选与对照按钮，一次请求、最多 4 次模型调用，完整回答仅保存在忽略的私有输出目录。
- 工作流验收脚本前两轮失败保留在 `real-workflow-go-release-20261119` 和 `-r2`：首次等待函数把 `ready` Promise 的完成值 `undefined` 当作未就绪；修为检查 Promise 是否存在后，第二次又提前点击刷新前的历史按钮。现改为等待新 revision 文本渲染后再点击。两轮真实上传／同步均成功，不把验收脚本超时混作业务通过。
- 第三轮 `real-workflow-go-release-20261119-r3`：真实 PDF 经 Zotero 导入后成功上传1次、同步文献/笔记2项；原生保存本地笔记、API修订为v2、原生历史显示v1/v2均通过。随后原生精析提交1个任务，状态失败；远端健康检查成功，正在定位实际解析失败原因，尚不能宣称解析全链路通过。验收笔记是隔离流程标记，不作为科研问答或训练数据。
- 解析故障依据：同一图片路径长289字符，普通 `Path.is_file()` 为 false，Win32 `\\?\` 前缀访问为 true；MinerU 已生成文件，读取阶段误判缺失。现对 Windows 缓存目录/图片统一扩展路径，同时保留 resolve 后的目录包含校验；新测试用真实深目录验证可读并拒绝目录外图片，连同现有缓存测试4项通过。该 Python 修复晚于 `6929f3b` release tag。
- Python core/worker/长路径回归共30项完成（1项依赖条件跳过）；CI 增加 worker 和深缓存路径契约。此处不将 Linux CI 当作 Windows 长路径实机验证。
- 第四轮 `real-workflow-go-release-20261119-r4` 已通过：干净 `6929f3b` Go EXE + 修复后 Python worker，真实PDF上传1次、同步2项、笔记v1/v2历史渲染成功，单个 MinerU 任务 `succeeded`、`cached=false`。未调用问答模型；解析使用真实 MinerU。此前失败缓存也已只读恢复为116块、11张图片。未将私有文献、数据库或个人记录上传。
- 验收提交 `43f92cb` 推送两次遇到 GitHub 443 TCP 超时；DNS 能解析但连通性检查失败，稍后正常重试成功，未修改网络/代理全局配置。
