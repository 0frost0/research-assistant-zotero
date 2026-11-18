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
- 生产仍使用 `hybrid_rerank`，本轮没有生产部署授权。检索完整职责、网页全部接口以及真实解析/上传仍是后续迁移工作。
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
