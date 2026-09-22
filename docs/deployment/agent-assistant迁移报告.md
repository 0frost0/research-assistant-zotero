# agent-assistant 迁移报告

日期：2026-09-20
结论：科研助手已迁移到 `agent-assistant:/srv/research-assistant/app`，本机 `127.0.0.1:8765` 已切换为 SSH 隧道入口。迁移期间未调用 DeepSeek 等付费模型。

## 1. 部署结构

| 组件 | 地址 | 运行环境 |
| --- | --- | --- |
| Web | `127.0.0.1:8765` | `.venv` |
| Qdrant | `127.0.0.1:6333` | 项目内校验后的二进制 |
| Qwen 嵌入 | `127.0.0.1:30001` | `.gpu_env` |
| MinerU | `127.0.0.1:30000` | `.gpu_env` |

四项均由 user systemd 管理，状态为 `active` 和 `enabled`，用户 linger 已开启。服务不监听公网地址；Windows 通过 `scripts/start_agent_assistant_tunnel.ps1` 建立 `8765` 隧道。

数据位于 10 块 20GB 数据盘组成的 RAID5 `/dev/md0`，挂载到 `/srv/research-assistant`。最终阵列为 `[10/10] [UUUUUUUUUU]`，文件系统约 176GiB，已用 27GB，剩余 141GB。系统盘未格式化。

## 2. 迁移内容与完整性

项目归档共 921 个文件，SHA256：

```text
ee6b4521eb7f0583fae88ee49abefc72d2321827c55c3d089f90c2bb8870b907
```

只迁移两个已授权模型，均来自旧服务器，没有公网重下权重：

| 模型 | 服务器路径 | 权重 SHA256 |
| --- | --- | --- |
| Qwen3-VL-Embedding-2B | `deployment/embedding/models/Qwen3-VL-Embedding-2B` | `c73fa9caeddeb3ff831d46c085a7a5708343248ca777e90f2d486964464509c1` |
| MinerU2.5-Pro-2605-1.2B | `models/OpenDataLab--MinerU2.5-Pro-2605-1.2B/snapshots/master` | `abf8681ca63b8dec7b67de257af47b821f179442f72998d0696ae2ed9232a5f0` |

Qdrant 1.19.1 二进制 SHA256 为 `7951936099f2bdc05776f875f5b683fe49beeb8c8ad71e4ff7ae042e984efc83`。集合 `research_assistant_multimodal_v1_2048_78a89162b0` 有 4169 点，优化器正常。状态显示 `grey` 是因为点数低于 `indexing_threshold=10000`，当前采用 full scan；真实检索已通过，不能写成 `green`。

本机与服务器最终复核：5 组正式业务数据库逐表计数和行内容哈希一致，4 个资料文件按内容哈希一致；6 个 SQLite 数据库 `quick_check=ok`。当前主要数据为 3 份资料、4037 条文字证据、132 个视觉资产、2 篇阅读登记、3 个原文/译文产物、125 张结构表和 2890 个单元格。

## 3. 环境与资源

- 主环境：Python 3.10，`numpy==2.2.6`、`scikit-learn==1.7.2`。
- GPU 环境：`torch 2.10.0+cu128`、`vllm 0.19.1`、`mineru 3.4.5`、`transformers 4.57.6`。
- 翻译环境：`pdf2zh-next 2.9.0`、`BabelDOC 0.6.2`、`PyMuPDF 1.25.2`、`openai 3.10.0`。
- 对齐环境：`onnxruntime 1.23.2`、`onnx 1.22.0`、`PyMuPDF 1.28.2`、`tokenizers 0.23.2`。
- RTX 5090 验证 CUDA 可用；Qwen 约占 4.9GB，MinerU 约占 13.9GB，总计约 18.7GB，32GB 显存仍有余量。

## 4. 验证结果

- 主测试 203 项通过，14 项按独立环境跳过；翻译适配器 11 项通过。
- 本地双语对齐真实推理成功，中文“患者”对齐英文原句，coverage 1.0。
- Qwen 中文向量为 2048 维、L2 norm 1.0；“心衰是什么”真实召回心衰指南第 10 页定义。
- 真实 smoke 覆盖中文文字检索、图片查询和 MinerU 合成 PDF 解析；MinerU 返回 2 个块并提取到 `Patients` 与 `85`。
- `/api/health`、`/api/status`、主页和 `/reader` 返回 200。
- 浏览器加载现有中文译文成功；中英双栏从第 1 页同步翻到第 2 页。唯一控制台错误是 favicon 404。
- 本轮付费模型调用数为 0；没有用模拟结果冒充新翻译质量验收。

## 5. 已处理问题与边界

- Python 3.10 中 SQLite 对多语句查询抛 `sqlite3.Warning`，应用现统一转换为稳定的 `ValueError`。
- MinerU 首次编译缺 `Python.h`，安装对应 Python 3.10 开发头文件后解决。
- 一次 `--help` 探针在解析参数前触发 ModelScope 默认下载，已立即终止并只清理本次产生的 373MB 缓存。正式服务同时设置 `MINERU_MODEL_SOURCE=local` 和显式本地模型路径。
- 翻译能力已配置，但自动全文翻译仍关闭；本轮没有验证新的真实付费译文。
- Qdrant `grey` 并非服务失败，但数据量增长超过阈值后应重新观察索引状态和延迟。
- 尚未执行整机断电后的自动恢复实验；`enabled` 与 linger 只能证明配置，不等同于断电验收。

## 6. 日常启动与回退

Windows 启动：

```powershell
cd D:\workspace\research_assistant
.\scripts\start_agent_assistant_tunnel.ps1
```

远端检查：

```powershell
ssh agent-assistant "systemctl --user is-active research-assistant-web research-assistant-qdrant research-assistant-embedding research-assistant-mineru"
```

本机项目和旧服务器服务均未删除，可用于回退。回退前必须先停止新入口写入，再用 SQLite backup API 获取一致快照；不要直接复制活跃 WAL，也不要让新旧两套网页同时接受写操作。模型和数据的后续复制、下载或删除仍需单独确认。
