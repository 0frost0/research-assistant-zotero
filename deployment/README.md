# 部署材料

`qdrant/compose.qdrant.yaml` 是本机 Qdrant 配置，文件迁移后仍使用 external 命名卷 `research-assistant-qdrant-data`，本次没有重建容器或迁移数据库。显式命令为：

```powershell
docker compose -f deployment/qdrant/compose.qdrant.yaml config
```

`embedding/` 中的两个 Python 文件是远程 Linux GPU 服务部署材料，不是本机网页的子模块。本次只整理本地源码，没有改远程运行目录或重启远程进程。

部署到远程时，把这两个文件一起放到现有远程服务根目录。服务按自身目录读取 `models/Qwen3-VL-Embedding-2B/`；启动器还使用同目录的 `.embedding_deps/`、PID 和日志。不能只把脚本移动到远程的新子目录、却把模型留在旧位置。不要直接在本机 Python 环境启动该 GPU 服务。

本机 SSH 转发脚本位于 `scripts/start_embedding_tunnel.ps1` 和 `scripts/start_mineru_tunnel.ps1`。这些脚本读取现有 SSH 配置并记录项目 PID；配置内容与原先一致。
