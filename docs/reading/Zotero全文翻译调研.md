# Zotero 全文翻译：结论与下一步

核实：2026-09-10。**目标仍是整篇中文 PDF，保留版式、可选择文字。划词翻译仅为补充。**

## 结论

优先参考 **zotero-pdf2zh**，继续使用现有排版引擎。它不是一种天然更便宜的新模型；费用取决于翻译服务、输入输出、重复请求和失败处理。

| 候选 | 实际输出／实现 | 对我们的价值 |
| --- | --- | --- |
| [zotero-pdf2zh](https://github.com/guaguastandup/zotero-pdf2zh) | 保留版式 PDF、双语 PDF；支持 pdf2zh 与 pdf2zh_next，默认后者 | 最贴合目标；与现有后端同源，不必搬进整个插件 |
| [Chikit-L/fulltext-translate](https://github.com/Chikit-L/zotero-fulltext-translate) | MinerU → 原翻译插件逐段翻译 → HTML 附件 | 可参考全文分段，不能替代保留原版式 PDF |
| [seasideccm/fullText-translate](https://github.com/seasideccm/zotero-fullText-translate) | PDF 转笔记、分段合并、翻译服务额度控制 | 可参考批次控制；README 仍有模板警告，不作为首选后端 |
| [MagicZotero](https://github.com/l0o0/MagicZotero) | 文档宣传 MinerU／pdf2zh 全文翻译及试用 | 未核实内部请求与实际费用，不推荐凭宣传迁移 |

## 真正影响 token 的机制

- **现有机制：** 本项目已有引擎缓存、非思考模式、有限重试，不能重复包装成新优化。
- **值得比较：** 旧 PDF2zh 使用较短的单段提示词并保留公式占位符，SQLite 缓存成功结果；相比 next 的结构化批次，可能减少提示开销，但段落数、译文质量与排版也会影响结果。尚未实测谁更省。
- **不能照搬：** Chikit-L 的活动流程遇参考文献标题后停止翻译，HTML 表格原样保留；每段最多尝试 5 次。它的“全文”不等于所有文字都已翻译，重试也不等于免费。
- **服务差异：** 传统机器翻译通常按字符／套餐计费，不消耗 DeepSeek token；本地模型没有云 API token 账单，但占用算力。零 DeepSeek 消耗不等于零成本，服务价格和科研质量需要另验。

## 推荐只做三件事

1. **先补全文账本与停止条件。** 每次请求记录输入、输出、缓存与未知用量；请求前预留预算，达到上限停止后续派发。价格未核实时只报告 token，不伪造人民币成本；已发出的请求仍可能收费。
2. **验证失败续跑。** 核对现有段落缓存是否稳定命中、哪些设置会使缓存失效；重试只补缺失内容。未完成时明确标为部分结果，不发布为完整译文。
3. **小样本比较再决定引擎。** 相同页面、模型、翻译范围，对比现有 next 与旧 PDF2zh 的请求数、提示开销、缓存、耗时、完整性及公式版式。保留旧产物，禁止为省 token 默默漏译表格或附录。

优先使用合成论文离线检查请求与故障；真实模型对照必须先确定调用预算。**本轮未安装插件、未更换后端、未发送文献、未调用付费翻译模型，也没有测得节省比例。**

## 可核对的源码

| 证据 | 固定位置 |
| --- | --- |
| Zotero 插件默认 next | [prefs.js:9](https://github.com/guaguastandup/zotero-pdf2zh/blob/35af40f5d206c1843e10bae000d9aaabf89096df/plugin/addon/prefs.js#L9) |
| DeepSeek 非思考适配 | [deepseek_thinking.py](https://github.com/guaguastandup/zotero-pdf2zh/blob/35af40f5d206c1843e10bae000d9aaabf89096df/server/utils/deepseek_thinking.py) |
| HTML 流程的跳过／重试 | [TranslationAdapter.ts:77](https://github.com/Chikit-L/zotero-fulltext-translate/blob/0990def1fa811c8d789f3dd296efc33a9bea9aa7/src/modules/TranslationAdapter.ts#L77)、[最多五次:307](https://github.com/Chikit-L/zotero-fulltext-translate/blob/0990def1fa811c8d789f3dd296efc33a9bea9aa7/src/modules/TranslationAdapter.ts#L307) |
| 旧 PDF2zh 短提示／缓存 | [translator.py:89](https://github.com/PDFMathTranslate/PDFMathTranslate/blob/d949a4b59e8ec2441312069c3fa605c34eb40fd1/pdf2zh/translator.py#L89)、[cache.py](https://github.com/PDFMathTranslate/PDFMathTranslate/blob/d949a4b59e8ec2441312069c3fa605c34eb40fd1/pdf2zh/cache.py) |

旧 PDF2zh OpenAI 路径也有 100 次限流重试上限及 SDK 默认重试，不能直接换装；这是源码上限，不是已发生或已计费次数。前三个插件标注 AGPL-3.0，复用代码仍需履行适用许可义务，独立进程不自动豁免。
