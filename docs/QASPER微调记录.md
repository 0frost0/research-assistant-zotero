# QASPER 真实论文数据微调

> 研究对象纠偏：用户明确原论文为 SetR。QASPER 是助手自行选用的额外科学问答数据，不是 SetR 使用的数据；本记录仅保留一次迁移探索，**不得作为 SetR 复现或第二阶段互补性目标的验收**。SetR 原始训练／测试协议见 [第二阶段纠偏](证据集合选择第二阶段.md)。

2026-11-10。本轮按用户要求直接下载公开论文证据数据并完成一轮微调；合成 smoke 不再作为第二阶段成果。**真实训练、修正缓存后的内部 dev v2、官方保留集 v2 均已完成并审计。当前 adapter 未超过强基线，不部署。**

## 来源、许可与标签

- [QASPER](https://allenai.org/data/qasper)，Dasigi et al., NAACL 2021，[论文](https://arxiv.org/abs/2105.03011)；原始论文文字来自 S2ORC，问答与支持证据由 NLP 从业者标注。
- v0.3 官方 archive：`https://qasper-dataset.s3.us-west-2.amazonaws.com/qasper-train-dev-v0.3.tgz`。公开 dataset card 标明 CC BY 4.0；原包、card 和哈希保存在 `output/qasper-v03/raw/`。不把数据复制进源码或项目日志。
- 不是自动生成的问答。模型输入只有原问题、已选原文、新候选；不包含答案文本、人工标签、证据 ID 或来源 ID。
- 监督目标：人工支持段落作为证据需求代理；重叠原文窗口可以复用已覆盖需求，由此产生状态依赖的冗余负例；同论文 BM25 候选中未被任一标注员选作证据的段落是**弱负例**，不是已证明无关。
- QASPER **不提供语义维度 gold**。不能把“支持段落召回提高”表述成“方法／数字／条件维度已提升”。多个标注员的支持集合分别保留，训练覆盖目标使用其并集，可能要求了实际可替代证据；官方 QA／evidence F1 与本项目集合指标不同，不能直接比较榜单。

## 数据构造与隔离

构建器：`benchmarks/prepare_qasper_selector.py`。

- 扫描官方 train＋dev 共 3,595 道问题、1,169 篇论文。
- 选取 2–6 个不同文字支持段落的问题，排除不可匹配原文、图表依赖、无支持证据、候选过少的问题。证据完整段超过 2,000 字符时剔除，**不裁切原文**。
- 800 个有效问题：467 train、57 internal dev、276 held-out test。原官方 train 888 篇按论文哈希分 796／92；官方 dev 281 篇整体映射 test。有效问题数不等于有效论文数。
- 18,246 个增量样本：train 14,814、dev 545、test 2,887；总正例 7,478，负例 10,768。GPU 训练只读 train，训练前后分类诊断只用内部 dev 固定样本。
- 有效问题实际来自 train／dev／test 的 338／39／182 篇论文。训练正例 5,803、负例 9,011，其中 1,604 个是有支持内容但已被已选集合覆盖的冗余负例；10,758 个训练样本的已选集合非空。独立重建的 groups/examples 与冻结数据完全相同。
- 去除跨 split 归一化完全重复原文 339 次；超长原文剔除 211 次。无近重复／语义改写查重保证。
- 训练池允许补入漏召回 gold，另添加含整段原文的相邻窗口以形成真实文本重叠；**内部 dev／held-out test 均为不注入 gold 的 BM25 Top-20**，必须单独报告候选召回上限。
- 本轮检索范围是 QASPER 问题指定的论文，不是 Zotero 全库检索；不能据此宣称解决跨论文召回。
- QASPER 没有 PDF 物理页定位；`page` 在适配 schema 中是段落序号，带 `locator_type=paragraph_ordinal_not_pdf_page`，不得拿它当 Zotero 跳页数据。

冻结派生数据 SHA-256：`632731196066c9e35e9b31daf73f7976f238396538222d026356879d14c4c047`。

## 第一轮训练配置与产物

- 固定 `Qwen/Qwen3-Reranker-0.6B`，revision `e61197ed45024b0ed8a2d74b80b4d909f1255473`。
- LoRA `q_proj/v_proj`、r=8、alpha=16、dropout=0.05；BF16、AdamW、学习率 `2e-5`、batch 4、梯度裁剪 1、seed `20261110`。
- 二分类 `no/yes` logits 交叉熵；不训练生成回答；左 padding，最后有效位置；使用与离线集合选择一致的输入模板。
- 目标 1 epoch，硬上限 10,000 steps，步间检查 7,200 秒训练预算。完整输入超过 2,048 token 的样本整条排除并计数，不截断。分类诊断固定最多 256 个内部 dev 样本；它不是 Top-6 集合效果。
- 远端专用 `.selector_env`；代码与数据放 `runtime/qasper-selector-20261110`，不覆盖生产源码，不改 reranker 服务或正式默认策略。
- 远端训练产物：`output/qasper-lora-v1/`，包含 adapter、tokenizer、manifest；训练日志 `output/training-v1.log`；有效同池报告为 `output/qasper-lora-{dev,test}-v2.json`。日志只记步骤、数值、依赖和错误，不记论文原文或凭据。
- 训练后从全新基础模型重载 adapter 核验；内部 dev 比较固定 Top-K、启发式、未训练 Qwen Top-K、Qwen＋启发式、未训练 Qwen 集合 prompt 和 LoRA 集合选择，最多 6 条／12,000 字符。

## 复现

```powershell
$py = 'D:\miniconda3\envs\deepagents\python.exe'
& $py benchmarks/prepare_qasper_selector.py `
  --raw-dir output/qasper-v03/raw --output output/qasper-v03/selector-v1.json
& $py -m unittest tests.test_qasper_selector tests.test_evidence_selector tests.test_qwen_stage1
```

新建输出文件和目录，拒绝覆盖冻结数据。远端在隔离副本目录执行：

```bash
ROOT=/home/researcher/workspace/research-assistant-zotero-github
cd "$ROOT/runtime/qasper-selector-20261110"
CUDA_VISIBLE_DEVICES=0 "$ROOT/.selector_env/bin/python" benchmarks/train_evidence_selector.py \
  --dataset output/qasper-v03/selector-v1.json --output output/qasper-lora-v1 \
  --base-model "$ROOT/models/Qwen3-Reranker-0.6B" \
  --model-manifest "$ROOT/runtime/reranker-model.json" \
  --epochs 1 --max-steps 10000 --max-seconds 7200 --max-length 2048 \
  --batch-size 4 --learning-rate 0.00002 --validation-examples 256
CUDA_VISIBLE_DEVICES=0 "$ROOT/.selector_env/bin/python" benchmarks/evaluate_evidence_selector.py \
  --dataset output/qasper-v03/selector-v1.json --output output/qasper-lora-dev-v2.json \
  --base-model "$ROOT/models/Qwen3-Reranker-0.6B" \
  --model-manifest "$ROOT/runtime/reranker-model.json" \
  --adapter output/qasper-lora-v1 --split dev
CUDA_VISIBLE_DEVICES=0 "$ROOT/.selector_env/bin/python" benchmarks/evaluate_evidence_selector.py \
  --dataset output/qasper-v03/selector-v1.json --output output/qasper-lora-test-v2.json \
  --base-model "$ROOT/models/Qwen3-Reranker-0.6B" \
  --model-manifest "$ROOT/runtime/reranker-model.json" \
  --adapter output/qasper-lora-v1 --split test
```

## 第一轮结果（已完成）

训练完成：1 epoch、3,704 steps、14,814 个 eligible train examples、0 个超预算训练样本；GPU 为 RTX 6000 Ada，峰值分配显存 2,170,044,928 bytes。LoRA 可训练参数 1,146,880；adapter 从全新基础模型重载核验通过；未修改生产服务。训练 loss 是 `1.0053→0.00435`，只作训练过程记录，不作为泛化证据。

固定的内部 dev 分类诊断（256 条）从训练前到训练后：loss `1.9884→0.8947`，positive recall `0.4016→0.6230`，negative recall `0.6567→0.8507`，balanced accuracy `0.5292→0.7368`。

以下为 57 个 internal-dev 问题、39 篇论文的 **v2 复验结果**：修正缓存键后完整重跑，选集／汇总数值与 v1 一致；v1 保留为历史，不再用于交付。

| arm | candidate dimension recall | coverage@6 | available coverage@6 | all support@6 | off-target |
| --- | ---: | ---: | ---: | ---: | ---: |
| fixed Top-K | 0.7877 | 0.3711 | 0.4728 | 0.1228 | 0.8480 |
| heuristic untrained | 0.7877 | 0.3623 | 0.4631 | 0.1053 | 0.8509 |
| untrained Qwen Top-K | 0.7877 | 0.5737 | 0.7074 | 0.2982 | 0.7661 |
| untrained Qwen set prompt | 0.7877 | 0.5196 | 0.6673 | 0.2632 | 0.7865 |
| **Qwen LoRA selector** | **0.7877** | **0.6409** | **0.8285** | **0.3509** | **0.7310** |

v2 相对未训练 Qwen Top-K 覆盖差值 `+0.0673`，按论文聚类 bootstrap 95% 诊断区间 `[-0.0109,0.1433]` 跨 0；13 题改善、6 退步、38 持平。相对未训练集合 prompt 差值 `+0.1213`，区间 `[0.0432,0.1989]`，20 改善、6 退步、31 持平。相对 fixed Top-K 差值 `+0.2699`，区间 `[0.1763,0.3522]`。这些是小型内部 dev 的方向性证据，不是总体收益承诺。

v1 重算选择指标通过，但审计器当时只验证已选 ID／预算／集合指标，未验证基线打分所属候选池，无法发现缓存串分。内部 dev 只有 35/57 题的候选池包含全部支持段落；缺失候选不能由 LoRA 补回。语义维度尚未评测，也尚未做“微调模型忽略已选状态”的消融，不能把精排适应收益直接归因于学会互补性。

### 评测失败与修正

官方保留集 v1 失败于 `invalid reranker scores`。诊断确认 276 题的模型原始分数均合法，实际原因是缓存只以问题字符串为键：不同论文同问题覆盖分数，其中一对候选数 15／20 导致数量错误；同长度冲突则会静默串分。保留集共 5 对重复问题，内部 dev 1 对。初步“瞬态非法分数”的判断错误，已撤回。

修复为问题＋有序完整候选文本的 SHA-256；新增同问题不同池／顺序回归，不重试、不放宽非法分数校验；失败轮不计结果。数据／adapter 不变。v2 报告使用新路径 `qasper-lora-{dev,test}-v2.json`；旧 v1 与审计保留作为失败记录。内部 dev v2 审计 `valid=true`；本机报告 `output/qasper-v03/remote/qasper-lora-dev-v2.json`，审计 `output/qasper-v03/remote/dev-audit-v2.json`。adapter 已另存本机 `output/qasper-v03/qasper-lora-v1/`，未发布或接入生产。

## 官方 dev 映射 held-out test 最终结果

这是官方 QASPER dev 的过滤子集，不是 QASPER 官方 test，也不是独立人工语义维度 gold；本轮训练和配置选择未使用它。276 个问题、182 篇论文，候选池固定为不注入 gold 的 BM25 Top-20。

| arm | candidate support recall | coverage@6 | all support@6 | off-target |
| --- | ---: | ---: | ---: | ---: |
| fixed Top-K | 0.7763 | 0.4092 | 0.1667 | 0.8273 |
| heuristic untrained | 0.7763 | 0.4068 | 0.1594 | 0.8285 |
| Qwen + heuristic | 0.7763 | 0.5507 | 0.2645 | 0.7693 |
| untrained Qwen set prompt | 0.7763 | 0.5556 | 0.3116 | 0.7645 |
| **Qwen LoRA selector** | **0.7763** | **0.5562** | **0.2899** | **0.7597** |
| **untrained Qwen Top-K** | **0.7763** | **0.5813** | **0.3152** | **0.7554** |

- **强基线没有被超过**：LoRA coverage@6 `55.62%`，低于原始 Qwen Top-K `58.13%`，差值 `-0.0251`；按论文聚类 bootstrap（5,000 次，seed 20261110）95% 区间 `[-0.0635,0.0125]` 跨 0。不能称统计上确认退步，但无收益依据。
- 相对未训练集合 prompt 仅 `+0.0006`，区间 `[-0.0402,0.0398]`；相对 fixed Top-K 为 `+0.1470`，区间 `[0.1026,0.1911]`，不能用弱基线收益代替强基线验收。
- 全部支持覆盖：LoRA `80/276`，原始 Qwen Top-K `87/276`，未训练集合 prompt `86/276`。
- 逐题配对 coverage：相对原始 Qwen 为 **60 改善／62 退步／154 持平**；相对未训练集合 prompt 为 **73 改善／62 退步／141 持平**。全部退步 ID 和逐题指标保存在 `test-audit-v2.json`，不复制论文原文。
- 候选边界：只有 `157/276` 题的候选池包含全部支持段落；LoRA 对已召回支持的选择召回 `0.7233`，低于原始 Qwen `0.7456`。62 个退步题中 **42 题全部支持段落已在候选池**，说明失败不全由召回不足造成。
- 可替代标注的自定义 best-annotation F1：LoRA `0.3097` vs 原始 Qwen `0.3063`；但 best-annotation recall 为 `0.6942` vs `0.7205`。不能挑 F1 单项宣称成功；这些不是官方 QASPER 分数。

### 失败样本与未验证归因

以下三题候选中支持齐全，原始 Qwen coverage 为 1，LoRA 为 0：

| group ID | paper | 支持段落数 |
| --- | --- | ---: |
| `Q5bb3c27606c59d73fd6944ba7382096de4fa58d8` | 1912.13337 | 2 |
| `Q7ab9c0b4ceca1c142ff068f85015a249b14282d0` | 1908.07245 | 2 |
| `Q97d0f9a1540a48e0b4d30d7084a8c524dd09a4c3` | 1910.06592 | 3 |

已观察到训练／评测候选分布不同：训练池补 gold 且有重叠窗口，dev/test 是自然 BM25 段落池；训练主要使用人工支持状态，推理使用模型自身选集。它们是待验证的失效假设，不是已证明的因果结论。尚缺“微调模型忽略已选状态”的消融；dev/test 六臂冗余率全为 0，本评测不能验证去冗余能力。未证明方法／条件／数字等语义维度更全面。

### 审计、复验与交付判定

test v2 本机审计 `valid=true`、0 errors，覆盖所选 ID、同池 membership/hash、6 条／12,000 字符预算、指标重算和配对统计；**不独立验证原始打分缓存归属**。远端实际执行源码哈希单独保存于 `output/qasper-v03/remote/run-source-hashes-v2.json`；远端运行版未包含本机后来新增的 `evaluation_protocol/evaluator_sha256` 字段，也未追写冻结报告。schema 中 `independent_test=false/effectiveness_claim=false` 是保守固定字段，不应误读为 test 参加了本轮训练。

- 本机报告／审计：`output/qasper-v03/remote/qasper-lora-test-v2.json`、`output/qasper-v03/remote/test-audit-v2.json`。
- test report SHA-256：`0ca9e86b6e30505239c3221e4e6afd210223410618b657bbe21e6494efdadfc3`。
- adapter manifest SHA-256：`a967153cdad3cda386170a6dbb402e966d0668255f2b336fb2067fe734874c82`。

```powershell
& $py benchmarks/audit_selector_report.py `
  --dataset output/qasper-v03/selector-v1.json `
  --report output/qasper-v03/remote/qasper-lora-test-v2.json `
  --output output/qasper-v03/remote/test-audit-v2-recheck.json
```

**决定：真实训练与本轮对照实验已完成，但“比原始 Qwen 选得更全面”的目标未达到。保留 adapter 和所有历史，不部署、不替换 `hybrid_rerank`、不接入 Zotero。** 如果继续，先只在 train/internal dev 做状态消融和去冗余诊断；本 test 已查看，任何据此修改训练／数据／超参数后的验收必须使用新的独立评测集。
