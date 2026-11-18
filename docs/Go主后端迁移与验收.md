# Go 主后端迁移与验收

更新：2026-11-18。Go 负责产品 API、SQLite 数据、revision、任务状态、问答编排、反馈、备份和解析队列；Python 保留模型调用、PDF/OCR/MinerU 以及当前检索索引 worker。当前不能称为“Python 仅模型/OCR”，因为检索仍由 worker 提供。

## 已验证

- `backend/` 使用 `modernc.org/sqlite`，保留 Python 的知识库身份、同步指纹、不可变 revision 和备份兼容性。
- Go 契约测试覆盖 Python→Go→Python 往返、并发 revision 冲突、同步回滚、任务取消优先于迟到结果、重启中断、重试上限、配置原子更新、附件校验、备份恢复和 worker 断连取消。
- Go 原生 Zotero 隔离验收：`output/zotero/real-questions-go-aligned-20261118/result.json`。五题通过实际工作台提交：四题有回答（EHR、RePrompT、MOTOR、个人记录），火星预算题为 `no_evidence`；10 次模型调用，引用审计 8 条、未匹配 0 条。个人记忆本轮为 `answered`，结果可能随模型调用变化。
- Python 对照验收基线为 `output/zotero/real-questions-python-json-20261118/result.json`：10 次模型调用，12 条引用未匹配 0 条；两轮不是准确率或人工 gold。
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
- 多文献对照已完成 Go 编排与合成测试，保留按篇取证、并行分析、跨来源约束和一次复核；尚未完成真实 Zotero 对照验收。
- 生产仍使用 `hybrid_rerank`，本轮没有生产部署授权。检索完整职责、网页全部接口以及真实解析/上传/对照仍是后续迁移工作。
- 引用审计只证明逐字摘录和页面/revision 对得上，不证明语义正确率、充分性准确率或泛化效果。

## 发布与 CI 故障记录

- 提交 `6929f3b` 首次推送两次发生 GitHub 443 TCP 超时；后续 DNS、TLS 和 `ls-remote` 恢复，正常推送成功，远端 SHA 已核对。
- 首轮远端 CI 的 Python、Node、XPI job 成功，Go job 在测试阶段失败。Go job 缺少 Python 环境与存储契约所需的 `pypdf`；现补显式 Python 3.12、pypdf 与 `PYTHON_EXECUTABLE`，Go 缓存路径改为 `backend/go.sum`。修复后的远端结果另行核验。
- `v0.4.0-go-core` 是核心迁移 prerelease。初始 Windows 包由未提交工作树构建，内嵌 `vcs.revision=a4c0c3f`、`vcs.modified=true`，不能以文件名声称它是干净 `6929f3b` 构建；后续提供独立标注的干净提交构建，保留初始资产字节供审计。
