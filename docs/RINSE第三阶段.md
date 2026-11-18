# 第三阶段：优先复用官方 RINSE

2026-11-13。用户已确认进入生成前证据充分性核验，优先复用官方代码和权重。**已完成官方特征级数值复现，尚未具备对新问题／原文打分的完整 reader。** 不提交、不推送、不部署，不改变生产 `hybrid_rerank`，不另做提示词版冒充复现。

## 1. 官方来源与固定版本

- [论文 v1](https://arxiv.org/html/2609.37469v1)、[TeX 源文件](https://arxiv.org/src/2609.37469v1)。源码中的 `github.com/TODO/RINSE` 是占位符，不是真实地址；不能据此断言作者没有发布代码。
- 从[第一作者主页](https://sutingchen.dev/)关联的 [GitHub 账号](https://github.com/caoster?tab=repositories)找到作者仓库：[caoster/RINSE-reproduction](https://github.com/caoster/RINSE-reproduction)。不是第三方同名项目。
- 固定 commit：`6c507525b5466b377fd37d1dc73d127006effba7`。`git archive` TAR SHA256：`fb12ff083d8f3a7df1351aab745c6380706d5f115d7462866227fc510eb24fe4`。
- GitHub 搜索 API 返回 403 rate limit；网页检索未找到不作不存在的证据。HF 模型／Space 同名检索未确认论文权重，dataset 查询只读了第一页，不宣称穷尽检索。最终可用性结论以固定仓库 README 和文件树为依据。
- 固定版本共28个文件，含8个 feature NPZ、12个 result NPZ。未发现 LICENSE／COPYING；README 未授予明确代码再分发许可。暂只在隔离目录运行，不复制到项目源码、不集成发布，后续须确认许可。

## 2. 已运行与已验证

直接运行未修改的官方 `verify.py`，不是自己实现打分替代官方代码。

- 本地兼容性验证：Python 3.11.15、NumPy 2.4.6、scikit-learn 1.9.0，退出码0。
- 随后远端新建独立 CPU venv：**Python 3.12.3、NumPy 2.5.1、scikit-learn 1.9.0**，满足官方声明；SciPy解析为1.18.1（官方未固定此传递依赖），退出码0。
- 重新拟合各留出 family 的 content head、数量修正及标准化；74,841条重算分数与冻结参考的最大绝对误差 **3.11e-09**，通过官方1e-5容差。
- 组内成对排序、平局半分、2,000次整问题组 bootstrap：主结果表及增加干扰段结果表的显示精度／区间全部通过官方检查。仅报告通过，不将论文 PA 解释成项目充分性分类准确率、IC@6或新数据收益。
- 未运行 reader 神经网络、未调用付费 API、未用 GPU、未读取 MultiHopRAG gold、未训练新模型。两环境结果一致不代表独立语义验收。

产物：

- 本地：`C:\Users\Researcher\AppData\Local\Temp\opencode\rinse-official-audit-20261113\`，含固定仓库、源文件核查、`upstream-manifest.json`、两环境日志／收据。
- 远端：`/home/researcher/workspace/research-assistant-zotero-github/runtime/rinse-official-v1-20261113/`，含 `upstream/`、`env/`、`install.log`、`verify.log`、`exit.json`。
- 远端完整验证日志 SHA256：`bb0cc6669616e96d0da2167047421fbc1b80f8fca70f7724c42e501051f0c3ab`。未更改已有 selector／生产解释器。

远端复跑命令（现有固定目录）：

```bash
CUDA_VISIBLE_DEVICES='' OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  /home/researcher/workspace/research-assistant-zotero-github/runtime/rinse-official-v1-20261113/env/bin/python \
  /home/researcher/workspace/research-assistant-zotero-github/runtime/rinse-official-v1-20261113/upstream/verify.py
```

## 3. 官方产物能做什么，缺什么

| 部分 | 固定版本实际提供 | 对新六段原文的缺口 |
| --- | --- | --- |
| 融合／评测 | `core.py`、`replay.py`、`verify.py`；训练及评测冻结特征 | 可直接复用方法与数值验证；不是原文推理入口 |
| Content | 训练／评测各两路9维覆盖特征，可重拟合18维判别头 | 未发布原文到覆盖特征的完整实现、精确 reranker/tokenizer revision、IDF及预处理产物 |
| Span | 已冻结的训练／评测 span 分数 | README明确不含 reader checkpoint；缺论文中NQ显式null训练后的RoBERTa及原文推理实现 |
| Latent | 各评测NPZ中含1536维 `direction`，另有已投影的train／padding／eval分数 | 方向**不是全部缺失**；但缺完整prompt、截断／token定位／reader实现与固定backbone revision，不能任取现有Qwen隐藏状态替代 |
| 构建数据 | 13,600条训练记录对应的匿名数值特征 | 不含原问题、原文、答案、原record ID及完整文本快照；不能从NPZ恢复原文训练闭环 |

`export_origin_features.py` 是来源说明工具，依赖未包含的原实验目录和 `f177_score` 等模块；它不是下载／生成reader的脚本。README明确说明从原文重建还需要原始文本快照、reader checkpoints等未再分发输入。

**当前阻塞不是GPU或安装问题，而是完整原文reader／权重／预处理产物及许可缺失。** 仅下载普通RoBERTa和Qwen基础权重不能补齐这些缺口；不能拿当前需求reranker分数冒充官方特征。

## 4. 最新决策：停止依赖作者私有产物

用户随后明确拒绝索要作者产物及自行准备训练数据。**不再推进下方旧计划中的联系作者、自建reader或自产标签。** 后续只考虑现成公开权重或公开可用数据；完整核查见 [充分性公开资源调研](充分性公开资源调研.md)：S2G存在真实LoRA但许可未明，MuSiQue-Full提供许可明确、已核验原文与标签的公开数据路线。本轮没有启动新训练。

### 历史执行门禁（已被上方约束取代，不是当前待办）

1. 优先取得作者原文reader实现、NQ span checkpoint、精确backbone/tokenizer revision、预处理／IDF和方向prompt配置，以及代码／权重许可；不擅自发送邮件、开issue或开启大规模训练。
2. 获得产物后固定commit／revision／文件SHA，先核查服务器已有权重，不重复下载；先做原协议原文→特征→分数smoke，再接固定六段。官方latent只看最高rerank五段，不能静默改成六段。
3. 原版输入保持question＋evidence，不读取生成答案；gold不进实际推理。保留现有 `research_assistant/knowledge/qa.py` 门禁作为后续接入点，当前不改其默认行为。
4. 部署阈值需独立开发／校准集，评测不足误放行率、可回答覆盖率、配对排序及三信号消融；留出真实论文／中文问题，不用已查看MultiHopRAG测试gold调阈值。
5. 作者无法提供时，按论文自建reader或替换现有模型是新的复现／适配范围，需先说明训练数据、算力、下载和偏差，再确认执行；不以已有数值复现冒充阶段三质量交付。

### 作者产物询问草稿（未发送）

> We verified commit `6c507525b5466b377fd37d1dc73d127006effba7` with Python 3.12 and the pinned NumPy/scikit-learn versions. The official verifier passes on all 74,841 records. We would like to evaluate RINSE on new question–evidence pairs, not only frozen features. Are the raw-text readers, the NQ-trained span checkpoint, exact backbone/tokenizer revisions, coverage/IDF preprocessing, latent prompt/state-extraction configuration, and artifact licenses available? We found the exported directions and can refit the fusion from the supplied numeric training features. Thank you.
