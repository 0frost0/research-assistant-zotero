# 版本归档与更新记录

2026-11-18：按用户要求补录中间产品。入口：[GitHub Releases](https://github.com/0frost0/research-assistant-zotero/releases)。全部新增版本标记为 prerelease；归档日期不冒充历史发布日期。

- 历史 `v0.1.0`、`v0.2.0`、`v0.3.0`–`v0.3.10`：上传实际留存 XPI、SHA256SUMS；标签源码从包内逐字节还原，只包含插件及归档说明，**不代表完整同期后端**。
- 原 `research` release 保留。它的 0.2.0 包与本地同版本包不同，`v0.2.0` 同时保留原发布包及命名为 `local-retained` 的变体，不覆盖历史。
- `v0.3.11`：本次完整源码与后端修复、插件包、回归记录、实验统计及简历草稿。源码发布不自动部署服务；只更新 XPI 不会安装 Python 后端、模型或数据库。
- 补充资产 `retained-development-bundles.zip`：42份原始中间工作流／后端包（有据问答、条件对照、个人记忆、门控、历史回看与重试阶段），附逐文件哈希。原包包含历史构建缓存；没有足够依据把它们一一对应到0.3.x插件版本，`final/verified` 等文件名也不代表本轮重新验收。
- 补充资产 `public-experiment-reports.zip`：11份公开充分性评分汇总、数据／预测指纹、研究负结果文档和合成记忆 smoke 汇总。可读副本位于 [artifacts](artifacts/)，仅凭汇总不能重放原始预测；重跑脚本与固定资源入口见实验文档。
- 归档工具：`scripts/archive_github_releases.py`。`prepare` 校验包 manifest、哈希和差异；`publish` 使用系统 Git 凭据，不写出密钥，不覆盖已有 release 资产。

| 版本 | 包内变更／阶段内容 |
| --- | --- |
| 0.1.0 | 初始 bridge、bootstrap、manifest |
| 0.2.0 | 分类工作台、文献与笔记同步；两份留存变体 |
| 0.3.0 | 个人记忆、问答历史、反馈、版本引用、本机设置的累积版本 |
| 0.3.1 | 递增版本区分旧设置窗口；留存包除 manifest 外与0.3.0相同 |
| 0.3.2 | Windows PathUtils 路径拼接 |
| 0.3.3 | SSH 可执行文件配置与 Windows 查找 |
| 0.3.4 | 曾加入 MinerU disabled，下一版撤回；保留失败设计 |
| 0.3.5 | 索引准备状态；恢复 MinerU local/remote |
| 0.3.6 | MinerU 转发、候选与入选计数、检索／纠正轨迹 |
| 0.3.7 | Zotero 10 分类接口、个人记忆轨迹、地址规范化提示 |
| 0.3.8 | Reranker 转发、算法与降级状态 |
| 0.3.9 | 实验图文问答、受控图片和视觉来源标签 |
| 0.3.10 | 实验充分性开关、检查轨迹与图片计费说明 |
| 0.3.11 | 后端严格决策类型、实验收据 CI、演示／简历与历史归档；插件逻辑未变 |
| 0.3.12 | 文本资产LF归一化，修复Git autocrlf导致的跨平台构建差异；保留0.3.11原始资产 |
| 0.3.13 | 历史回答回显已保存评分，取消问答后清除进度提示；Zotero 10.0.8 实测取消、重试和反馈回看 |

这些说明由真实包逐文件比对及历史故障笔记整理；同期后端改动不冒充 XPI 内的内容。实验负结果保留在 [公开资源调研](../充分性公开资源调研.md)、[QASPER](../QASPER微调记录.md)、[SetR](../SetR重跑记录.md)、[IC6](../IC6提升调研.md)、[RINSE](../RINSE第三阶段.md)。公开附件为汇总和指纹，私人原文、数据库、凭据与未授权模型权重不发布。

42份历史包的更新依据：`qa-release*` 为有据问答阶段；`comparison-release` 为条件对照阶段；`memory-release*` 为个人记忆与组合回答阶段，其中 `memory-gate`、`source-anchor`、`ci-memory` 分别对应门控、来源锚点和CI迭代；`research-assistant-memory*` 为记忆可靠性迭代；`research-assistant-release-qa-history*`、`ci-guard*`、`qa-eval-guard*`、`manual-retry*` 为历史回看、CI门禁、评测校验和手动重试阶段。同系列序号／final变体的精确更新以 `artifacts/retained-bundles.json` 逐文件哈希及包内代码／故障笔记为准，不推测每个后缀对应的功能或日期。

包内另留存4种不同字节的早期0.3.0插件（均为6个文件，尚无engine-settings资产），已随42包完整保留；不与 `v0.3.0` release 的后期8文件包混称同一构建。可用库存表中 `deployment/zotero/dist/*.xpi` 的哈希定位这些变体。

0.3.11的同工作区双构建一致，但干净Git源码构建为另一哈希，说明原检查未覆盖换行转换。0.3.12修复后推荐使用该版；0.3.11及其42包／实验附件仍可下载，历史资产不覆盖。0.3.12同样指向完整源码提交。

发布完成：15个新增prerelease；原research保留。前14版31资产实际下载核验通过；0.3.12干净源码构建、GitHub资产digest、更新清单及main/tag CI通过，后续再次下载遇到网络超时。详见 [历史资产收据](artifacts/releases-archive-verification.json) 与 [0.3.12收据](artifacts/release-0312-verification.json)。
