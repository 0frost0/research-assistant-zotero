# 本地主后端 + 远端 Qwen

状态：代码链路已接入，按要求未运行测试、安装依赖、启动服务、部署远端或请求模型。以下命令供后续启动使用。

## 本地启动

优先使用 Zotero「工具 → 科研知识库 → 本机启动设置」，填写 Python、启动脚本、项目目录。远端 Qwen 可填写已配置免密的 SSH 别名；如需代启远端服务，额外填写实际 systemd 服务名（需要该单元的免密 sudo 权限，不自动修改权限）。已常驻运行时服务名留空。

随后点击「启动全部服务」：启动已安装的 Docker Desktop／Qdrant，按配置请求远端启动 Qwen，建立 30001 SSH 隧道，启动本地主后端并自动读取连接令牌。MinerU 本地模式在精析任务中按需启动，不需要单独常驻端口。远端 MinerU 必须已有可用服务；本按钮不猜测或启动它的服务单元。

「高级连接设置」只是插件与 Python 后端的地址／令牌，和模型 API 不同。本机模式无需手填令牌。窗口应可在服务离线时打开；菜单异常现在显示具体原因。尚未在原生 Zotero 复验，不能声称已定位用户当前打不开窗口的根因。

依赖安装、GPU 驱动、模型文件及远端服务部署仍需预先具备；按钮不自动安装它们。退出 Zotero 只终止插件持有的引擎／隧道，不停止共享 Docker、Qdrant 或远端模型。下面终端命令仅保留作备用。

项目目录：`D:\workspace\research-assistant-zotero-github`。

```powershell
cd D:\workspace\research-assistant-zotero-github
conda activate deepagents
powershell -File scripts/start_local_storage.ps1
python scripts/start_local_engine.py
```

需要已安装 Docker Desktop 和项目 Python 依赖。存储脚本可能拉取 Qdrant 镜像，复用已有数据卷，不清空数据库。后端只监听 `127.0.0.1:8765`。

Zotero 的“配置引擎”：解释器填此环境 python.exe，启动脚本填项目 `scripts/start_local_engine.py` 绝对路径，工作目录填项目根目录。连接地址填 `http://127.0.0.1:8765`；令牌读取 `.data/zotero/connection-token`（若设置 RESEARCH_ZOTERO_TOKEN 则使用该值）。不要公开令牌。

SQLite、附件、解析缓存位于本项目 `.data/zotero/`；不会自动迁移原远端数据。Qdrant 地址仍由后端 `.env` 的 `RESEARCH_ASSISTANT_QDRANT_URL` 配置，默认本地 6333。

## 远端 Qwen API

在已有 Qwen 环境中更新 `deployment/embedding/remote_embedding_service.py`，保持原模型文件。新增兼容层复用官方 helper，**不是安装 vLLM**，仅支持本适配器使用的 messages 子集。旧 `/embed` 保留；新增 `/v1/embeddings` 需要 Bearer key。

在远端同目录执行 `python prepare_api_key.py` 创建密钥文件，再使用已有服务管理方式重启该脚本。模型路径可通过 `QWEN_MODEL_PATH` 指定；GPU 可通过 `CUDA_VISIBLE_DEVICES` 指定。原启动脚本中的固定 GPU UUID 是旧机器配置，不应直接照搬。

本地单独打开终端建立隧道（替换 SSH 别名）：

```powershell
powershell -File scripts/start_qwen_api_tunnel.ps1 -SshHost YOUR_SSH_ALIAS
```

Zotero 嵌入配置：地址 `http://127.0.0.1:30001/v1`，模型 `Qwen/Qwen3-VL-Embedding-2B`，维度 `2048`，密钥填远端 api-key 内容；同名权重变化须更新版本标识。服务保持远端回环监听，不开放公网端口。

## MinerU 与问答

- 问答：面板保存 OpenAI-compatible 的地址、模型、密钥；后续原文问答、个人记忆问答、复核及对照模型构造使用该配置。旧网页独立路径与翻译设置不在本次改动范围。
- MinerU 本地：选择本地 GPU，填写独立 MinerU 环境的 python.exe 绝对路径；该环境需已有支持 `pipeline -d cuda` 的 MinerU CLI、CUDA PyTorch 和本地模型。当前不自动安装或下载，具体环境兼容性待验收。
- MinerU 远端：本地仍需 MinerU 客户端环境，填 Python 路径和兼容 `vlm-http-client` 的地址（可通过回环 SSH 隧道访问）。不是任意文件上传 API。
- 同步先保存原 PDF 和快速文字。勾选“同步后自动精析”或点击“精析所选库”启动后台任务；任务显示阶段，不伪造百分比；可点击“解析任务”恢复查看，失败／中断后重新提交。
- 精析产物包含文字块、图片路径、图注、页码和 bbox。下一次检索按所选来源建索引；不会编码未选库的全部语料。不同来源组合使用独立索引，因此重叠选库可能重复编码，后续再优化逐内容缓存。
- 图像命中可回到 Zotero 原页。问答目前只用文字证据，不把图片定位信息当成视觉理解结论。

## 数据和未验证边界

解析失败保留原文与快速文字；解析不会覆写已有笔记。模型配置按任务启动时固定，不修改全局环境变量。备份保留原件和笔记，排除可重建 MinerU 缓存；恢复后需重新精析。

待运行：Python/JS 语法与离线回归、插件打包、原生 Zotero 操作、远端 API 图文请求、本地 CUDA MinerU 解析、任务中断和重启、费用与延迟。未宣称这些检查已经通过；也未切换实际运行中的旧服务。
