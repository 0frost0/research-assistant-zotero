# PDF 解析器第一轮对比
## 测试范围

- 测试文献：`MDAgent.pdf`
- 原文：43 页，约 7.45 MB
- 第一轮统一样本：第 1-10 页，约 3.70 MB
- 样本覆盖：标题页、普通正文、Figure 1-7、Table 1-3、数学公式和多种图表
- 设备：Windows，CPU-only，无 NVIDIA GPU
- 当前解析器：项目内 `PDFExtractor`（pypdf 6.16.1 + 按需 Tesseract）
- Docling：2.124.0
- MinerU：3.4.5，`pipeline + txt + formula + table`

第一轮只比较相同的 10 页，避免 MinerU 在 CPU 上一次处理 43 页导致等待失控。正式选择解析器前，应在同一设备上完成 Docling 的剩余实测。

## 定量结果

| 指标 | 当前解析器 | MinerU | Docling |
| --- | ---: | ---: | --- |
| 10 页耗时 | 0.546 秒 | 170.286 秒（模型已缓存） | 未完成 |
| 首次运行耗时 | 0.546 秒 | 265.754 秒（含约 1.01 GB 模型下载） | 模型下载受阻 |
| 输出字符数 | 44,074 | 46,549 | - |
| 英文单词数 | 5,742 | 6,413 | - |
| 标题结构 | 0 | 19 | - |
| 提取图片文件 | 0 | 17 | - |
| Markdown 图片引用 | 0 | 14 | - |
| 数学标记 | 0 | 212 | - |
| 结构化内容记录 | 10 个整页 Document | 111 个内容块 | - |
| 页面定位 | 页码 | 页码 + bbox | - |

MinerU 的 111 个内容块包括：80 个文本块、11 个 chart、3 个 image、3 个 table，以及页码、脚注和页脚。当前解析器词汇中约 90.39% 也出现在 MinerU 输出中，说明 MinerU 在增加结构信息时没有大面积丢失正文。

## 质量观察

### 当前解析器

优点：

- 速度快，10 页只需约 0.55 秒。
- 正文内容基本完整，逐页页码可靠。
- 原生文字 PDF 不需要模型，也不依赖网络。

问题：

- Figure 1 被拆成大量图内文字并混入正文，图片本身没有保存。
- Table 1 被压平成普通行，列关系丢失。
- 数学公式可读但下标、集合符号和排版信息丢失。
- `image_count` 只统计顶层 PDF 图片对象。第 2 页肉眼可见大型 Figure 1，但报告为 0 张图片；因此该值不能作为页面是否包含图片的可靠判断。
- 10 页都超过 80 个字符，因此全部被判定为“正常原生页”，但这并不能说明表格、图片、公式和阅读顺序正确。

### MinerU

优点：

- Figure 1 被裁剪为清晰的独立图片，图注紧跟图片。
- Table 1 同时保留了清晰表格截图和 HTML 行列结构。
- 正文标题形成 Markdown 层级。
- 公式转换为 LaTeX 风格标记，而不是完全压平。
- `content_list.json` 为每个内容块保存 `type`、`page_idx` 和 `bbox`，更适合建立 Evidence Store。
- 图表、普通图片和表格被区分为不同内容类型。

问题：

- CPU 热启动处理 10 页约 170 秒，是当前解析器的约 312 倍。
- Table 1 的图标单元格被 OCR 成错误字符；表格截图正确，但 HTML 中该行不能直接作为可靠数据。
- 部分公式虽然保留 LaTeX，但出现多余空格和个别错误符号，仍需要质量校验。
- Markdown 本身没有稳定的逐页分隔；页码需要从 `content_list.json` 合并回证据记录。
- 启动本地 API 服务占用约 40 秒，单篇短文档成本尤其明显。批量常驻服务会比每次启动 CLI 更合理。

### Docling

Docling 已成功安装，RapidOCR 模型也已下载，但标准流水线需要从 Hugging Face 获取 Heron 版面模型和 TableFormer 模型。当前网络出现两层阻塞：

1. Python/Hugging Face API 连接报 `SSL: UNEXPECTED_EOF_WHILE_READING`。
2. Git 镜像可以读取模型仓库元数据，但 Git LFS 下载跳转到 `us.aws.cdn.hf-mirror.org` 后 DNS 解析失败。

因此当前没有 Docling 的实际转换输出，不能根据官方宣传或别人的基准给它打分。待模型文件能完整下载后，继续使用同一份 10 页 PDF、CPU、标准 pipeline 和 referenced image 模式补测。

## 第一轮结论

1. “每页文字超过 80 字”只能判断页面不为空，不能判断 PDF 已被正确理解。
2. 当前解析器适合快速获取干净正文，不适合单独承担科研 PDF 的结构化解析。
3. MinerU 明显改善图片、表格、公式、阅读顺序和页面坐标，但 CPU 成本过高，不应在每次提问时运行。
4. 重型解析器应只在资料入库或文件变化时运行一次，结果缓存为结构化证据；问答阶段只读取缓存。
5. 暂时不能在 Docling 和 MinerU 之间做最终选择，因为 Docling 尚未完成同机实测。

## 推荐的项目策略

- 保留当前解析器作为快速路径和降级路径。
- 新文献入库时先做结构质量检查，而不是只检查字符数。
- 检测到表格、图片、公式、多栏或异常阅读顺序时，再进入 Docling/MinerU 重型解析。
- Evidence Store 至少保存：`doc_id`、`page`、`bbox`、`content_type`、`text`、`image_path`、`parse_method` 和 `confidence`。
- 表格同时保存结构化 HTML/JSON 和原图；OCR 结果与原图不一致时，不能让 Agent 直接把 OCR 内容当成事实。
- 普通问答仍使用确定性 RAG；图片或表格问题才按需调用视觉模型。

## 复现产物

- 基准脚本：`benchmarks/pdf_parser_benchmark.py`
- MinerU 模型缓存：约 1.01 GB，不属于项目源代码
- MinerU 隔离环境：`.benchmark_envs/mineru`
- Docling 隔离环境：`.benchmark_envs/docling`
- 两个隔离环境均不影响正式 `deepagents` 环境
