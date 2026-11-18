# SetR 第二阶段重跑

## 边界与状态

2026-11-12 更新。**共享 Qwen3.5-27B 已完成本地权重检查和640条冻结源标签生成：512 train／127 dev 合法，1条dev超长；结构门禁不代表语义质量达标。学生1／3／5 epoch均已训练、通过fresh reload，并完成全部2,556题预测及统一评测。源dev预选3轮，但三个端点IC@6均低于原Qwen reranker，不能作为成功模型部署；失败和诊断产物全部保留。** 独立教师环境 Transformers 5.3.0，学生保留旧环境；32B 下载不恢复，4B 旧标签不用。不提交、不推送、不部署，生产继续 `hybrid_rerank`。QASPER 与 Zotero 52 题不进入正式覆盖评测。后续IC@6召回／重排探索见 `docs/IC6提升调研.md`。

这是 **SetR-style 小模型适配**，不是原版 Llama-3.1-8B/GPT-4o 复现：目前使用项目 BM25、256 whitespace-word／64 overlap 分块、Qwen 教师和选择式 LoRA。不能与论文 BGE 候选池上的结果直接比较，也不代表已完成论文四个 QA benchmark 或端到端 QA 验收。

## 测试来源与原论文协议核查

2026-11-12 核查 [MultiHop-RAG 官方仓库](https://github.com/yixuantt/MultiHop-RAG) 与 [SetR v2 §4.1、§5.1、附录 A.3](https://arxiv.org/html/2507.06838v2#A1.SS3)：

- 问题、gold evidence 来自官方 `MultiHopRAG.json`，语料来自 `corpus.json`；不是项目自行编写的问题，也不是教师生成的测试标签。上游数据包含自动构造流程，不称为全人工 gold。冻结文件已记录 SHA256，但未核实下载时的上游 commit，不能声称固定了官方 revision。
- **同一 benchmark 不等于同一测试输入**：本项目自行按256 whitespace words／64 overlap分块并用项目BM25取Top-20；论文正文／附录采用BGE检索Top-20，主表另有BM25实验。尚未复现官方候选池、分块与检索实现。
- IC 的不同gold证据并集／完整gold集合分母与附录公式一致。论文说明用正则匹配并去重；本项目用ftfy后去全部空白、区分大小写的literal containment，不能称逐实现一致。2,255 answerable宏平均、301 null单列及失败零覆盖是本项目明确冻结的口径；论文相关段落未明确同样的null处理和聚合细节。
- 本轮仅评估MultiHopRAG证据选择覆盖，不是论文四个benchmark的完整端到端QA与检索指标复现。附录A.3明确一般多跳QA用Llama-3.1-8B-Instruct回答，**MultiHopRAG用gpt-4o-2024-08-06回答**，不能仅据正文概述说所有数据集均用Llama。
- 训练也不是原配置：约40K GPT-4o标签／Llama-3.1-8B的SetR，替换为512条Qwen3.5-27B标签／Qwen3-0.6B selection-only LoRA；selection-only对应简化变体，而非完整IRI推理训练。追加到5 epoch不能消除这些差异。
- 因此现有结果只支持冻结项目候选池上的内部对照，不直接对比论文绝对分数，也不能证明原版SetR方法无效。官方提示不限制选择数；IC@6是评测时截取输出前6段，不是训练时存在硬Top-6指令。gold不参与训练或候选召回；查看首轮结果后追加3／5轮仍属于探索性复测。

## 已验证

- 正式 1 epoch checkpoint 已通过 fresh-base reload：512 train、127 dev、64 steps，无长度溢出跳过；教师目标 dev loss `1.086469→0.521281`。这是目标拟合指标，不是外部证据质量证明；前两次诊断产物保留。

- MultiHopRAG 原始数据：2,556 题、609 篇文档；生成 5,636 分块，每题固定 Top-20，不注入 gold。
- 2,255 道有答案题参与 IC 宏平均；301 道 `null_query` 单独报告。完整语料中没有未匹配的 gold fact（固定 ftfy 6.3.1 后移除空白、区分大小写的 literal containment）。分块与候选池的漏召回仍计入分母。
- 官方 parsed 训练源成功下载：398,626,263 bytes，SHA-256 `54401048fb67224226b6438e0e57e8c7fe6e0d09bb3653cf3741cfbd726c6f1c`。
- 39,912 行只有 4,989 个不同问题；其中 9,978 行有 20 个候选，其他行有 2–19 个。先为每个问题确定一个 Top-20 版本，再按归一化问题／相同段落连通分量隔离，冻结 512 train／128 dev。**没有完成近重复语义审计**，不能将精确去重说成完全排除了训练污染。
- 源文件继承的 RankZephyr 回复全部丢弃，不当作 SetR 选择监督。补查发现官方许可证实际叫 `License.md`，是 BSD-3-Clause；此前只检查大写 `LICENSE` 导致漏查。代码许可证不等于底层 RankZephyr／MS MARCO 原文的再分发许可，原文与标签不上传。
- 最新协议／教师门禁／模型指纹测试 19 项通过；连同 BM25 契约测试共 37 项通过。官方 IRI prompt 已与固定 commit 的 Python 字符串逐字比较一致，审计文件为 `output/setr-v1/official-supervision-audit-v1.json`。
- 27B smoke：3/3 完成且严格 parser 合法，输出 1352／880／1222 tokens，单条 132.34／85.14／118.18 秒，选择 6／6／7 段。模型设备映射仅 GPU0/1，加载器记录加载 762 秒左右；当前无 FLA／causal-conv1d。完整生成最终 640/640，639 条 `ok`、1 条 `output_limit`。
- 原始输出定性检查（助手检查，不是独立人工 gold 标注）：能对应段落、识别同名干扰；但存在重复证据（CEO 样本 [6]/[18]）、非必要信息需求和对统计范围的推断。保留原输出，不修复／重排，不据此宣称语义收益；问题笔记不保存源段落。

## 固定实验计划

资源选择纠正：不再把“32B”视为必须精确匹配的参数规模。现用 `/storage/models/Qwen/Qwen3.5-27B`，11 个索引分片存在，合计 55,563,022,432 bytes、未配置量化；项目 runtime 建只读用途软链接，共享文件不改。配置为 `Qwen3_5ForConditionalGeneration`，因此使用 `AutoModelForImageTextToText`，仅输入文字，不把它强塞给旧 CausalLM loader。`verify_setr_model.py` 正在记录全部加载文件 SHA256、检查分片张量字节边界；只能建立本地身份，没有依据时不冒称上游 revision。

1. 教师为现有 Qwen3.5-27B，整组候选＋官方 SetR IRI prompt，greedy，`enable_thinking=False`，最多 4,096 输出 tokens，BF16 GPU 0/1、每卡权重预算 40 GiB、拒绝 CPU／disk offload；关闭隐藏思考不等于删去 prompt 要求的显式信息需求分析。先固定源 train 前 3 条做 smoke，检查原始输出后才运行 640 条完整生成。生成后 train/dev 各自至少 95% 语法有效方可进入学生训练；这是结构门禁，不是语义质量认证。替代教师不能称为 GPT-4o 监督或原论文复现。
2. 学生 `Qwen/Qwen3-0.6B`，revision `c1899de289a04d12100db370d81485cdf75e47ca`：**selection-only 蒸馏，不训练 IRI 推理文本**。LoRA q/v、r8／alpha16／dropout0.05，一轮，lr1e-4，累积8，seed20261110，最多 10,240 输入＋目标 tokens。训练阶段关闭 `use_cache`；fresh-base reload 也固定关闭该项后比较 logits。不把它称为原论文训练规模或原版监督。
3. 固定最终 checkpoint；dev 仅记录教师目标交叉熵，不能作为 gold 覆盖质量证明。不使用 MultiHopRAG 选择 checkpoint 或调参。训练只用其无 gold 问题副本检查精确问题重叠。
4. 对照：BM25 Top-6、原始 Qwen3-Reranker Top-6、未训练生成式 Qwen3-0.6B、学生 LoRA。模型输入副本没有 gold／答案／题型；整组选择保持模型原始输出次序。
5. 指标：candidate IC、IC@1/3/5/6、Hit、全部 gold 覆盖、已召回事实内 IC、标注命中段落比例、重复 gold 命中、选择数／字符预算、题型切片、null 空选择率。未标注段落不是已核对负例；标注命中比例不是语义 precision。
6. 不截断、不补齐、不修复非法输出、不回退 Top-K。格式错误／超长按零覆盖计入；只有合法空输出才算 null abstention。预测分片不完整则拒绝输出正式报告。

完整教师任务：该 runtime 的 `output/teacher-qwen35-27b-full-v1.jsonl`，640/640 已写入；结构审计为 `output/teacher-qwen35-27b-full-v1-audit.json`。smoke 原始文件保留，不直接拼入完整任务（run_id／split／worker 不同）。1 条 `output_limit` 不进入学生目标；其余 639 条通过严格 parser。该门禁只检查冻结 ID、输入指纹和输出格式，不证明语义证据质量。

重复 I/O 修正：生成器增加可选项目内 `--verification-receipt`。首次使用仍完整 SHA 校验并保存 size／mtime_ns／ctime_ns／inode／device／真实路径与 manifest 哈希，后续只有一致的大权重才复用；配置／模板始终 SHA 校验，变动则重新全量校验。旧任务没有保存校验当时的 stat，不能事后伪造收据，因此本轮完整任务仍需一次建收据校验。该优化只适用于可信非对抗文件系统，不能防恶意保留元数据的内容篡改，默认不传仍全量 SHA。阶段日志现明确区分校验／加载／生成。

## 已完成的 BM25 基线

文件：`output/setr-v1/bm25-baseline-v1.json`，2,556 题全部完成，以下 IC 仅以 2,255 道有答案题为分母。

| 指标 | 值 |
| --- | ---: |
| Top-20 candidate IC | 0.732779 |
| IC@1 | 0.210902 |
| IC@3 | 0.377864 |
| IC@5 | 0.476792 |
| IC@6 | 0.513525 |
| Hit（Top-6） | 0.854102 |
| 全部 gold 覆盖（Top-6） | 0.219956 |
| null 合法空选择率 | 0 |

这仅说明目前候选召回和固定排序的覆盖损失，**还不能说明学习式集合选择有收益**。

## MultiHopRAG 外部测试结果

2026-11-12。冻结 `multihoprag-bm25-v1.json`（2,556 题、2,255 answerable、301 `null_query`、BM25 Top-20 candidate pool）上的统一结果已完成。外部 gold 只在评测阶段读取，未用于训练、checkpoint 选择或生成约束。LoRA 使用固定一轮训练后的正式 checkpoint；没有额外调参。

| arm | IC@1 | IC@3 | IC@5 | IC@6 | all-selection IC | Hit | 全部 gold 覆盖 | invalid/overflow | 平均选择数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| BM25 Top-6 | 0.210902 | 0.377864 | 0.476792 | 0.513525 | 0.513525 | 0.854102 | 0.219956 | 0 | 6.00 |
| 原始 Qwen reranker Top-6 | 0.233629 | 0.448891 | 0.553215 | 0.593902 | 0.593902 | 0.904213 | 0.303769 | 0 | 6.00 |
| 未训练 Qwen3-0.6B 生成 | 0.207871 | 0.348263 | 0.423503 | 0.447339 | 0.593200 | 0.907761 | 0.286475 | 15 | 14.57 |
| Qwen3-0.6B LoRA | 0.211160 | 0.360680 | 0.440133 | 0.467443 | 0.615484 | 0.913969 | 0.319734 | 2 | 12.77 |

严格预算结论：LoRA 相对未训练生成模型 IC@6 `+0.020103`，但相对原始 Qwen reranker Top-6 `-0.126460`；相对 BM25 Top-6 `-0.046083`。LoRA 的 unrestricted all-selection IC 为 `0.615484`，高于未训练生成的 `0.593200`，但两者平均选择数分别为 12.77／14.57，不能与 Top-6 基线直接比较，也不能称为预算内收益。

LoRA 的格式失败从未训练模型的 15 条降到 2 条，但仍没有学会输出空集合：301 条 `null_query` 的合法空选择率均为 0；LoRA 在这些题上平均仍选择 5.89 段。answerable 的主分母保持 2,255，`null_query` 不混入 IC 分母。

题型切片的 LoRA IC@6：comparison `0.542640`（Qwen reranker `0.714369`）、inference `0.398182`（`0.491932`）、temporal `0.453974`（`0.559748`）。因此没有任何主要 answerable 切片超过原始 reranker；本实验不支持部署或替换 `hybrid_rerank`。

## 追加 3／5 epoch 实验（探索性）

2026-11-12 用户要求继续增加 epoch。单轮 IC@6 相对未训练生成模型提高 2.01 个百分点，值得继续验证训练预算；不足以用一次实验判定方法无效，也尚无重复种子／置信区间证明显著性。

- 新目录：runtime `output/epoch-sweep-v1-20261112/`，完整统一报告 `results.json` 已完成；旧 adapter、预测和报告不覆盖。
- 固定两个端点：3 epoch（192 steps）、5 epoch（320 steps），各从相同冻结基础模型重新初始化，不是给现有 adapter 再加 3／5 轮。512 train／127 dev、seed、lr1e-4、LoRA 配置、累积8、长度预算不变。
- 每轮记录 train loss 和教师目标 dev loss，下一轮显式恢复 `train()`；每个端点都验证 fresh-base reload，容差不放宽。
- `source-dev-selection.json` 在任何新外部预测之前写出：1／3／5 中仅按最终源 dev loss 最低选择，平局选更少 epoch。该标准衡量教师拟合，不是语义覆盖 gold。
- 两组都用原生成协议跑全部2,556题（greedy、selection-only、max_new_tokens=1536），严格 parser，不补齐、不修复；报告全部端点，不按外部结果挑胜者。复用原 BM25／reranker 基线及未训练／1 epoch 预测。
- MultiHopRAG v1 已被查看，追加实验是看到结果后的探索性复测；不得把这轮重测包装为全新独立验收。训练仍不读取 gold，也不使用其选择 checkpoint。生产继续 `hybrid_rerank`，不提交、不推送、不部署。
- 编排入口：`benchmarks/run_setr_epoch_sweep.py`；顺序训练3／5轮，然后GPU0／1并行生成并自动统一评测，避免再次只完成训练而漏评测。相关40项测试通过。

### 已完成端点与当前状态

3／5 epoch 均训练完成并通过 fresh reload，分别192／320步。源dev最终loss：1 epoch `0.521281`、3 epoch `0.495466`、5 epoch `0.510431`；外部预测前写出的选择文件选中3 epoch。5轮运行每轮train loss从`0.618177`降至`0.366822`，dev loss第2轮约`0.496648`后回升至`0.510431`，支持对教师目标的过拟合迹象，不构成唯一因果证明。

3 epoch已完整2556/2556（2554合法、2非法）；5 epoch已完整2556/2556（2551合法、5非法）。统一报告为 `output/epoch-sweep-v1-20261112/results.json`；早先的 `epoch5-complete-results.json` 仅保留为中间产物，不再等待旧任务：

| endpoint | IC@1 | IC@3 | IC@5 | IC@6 | all-selection IC | Hit（全选择） | 全gold（全选择） | 平均选择数（全2556题） |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 epoch | 0.211160 | 0.360680 | 0.440133 | 0.467443 | 0.615484 | 0.913969 | 0.319734 | 12.77 |
| 3 epoch（源dev预选） | 0.210347 | 0.358389 | 0.427790 | 0.452106 | 0.584257 | 0.901996 | 0.279379 | 12.60 |
| 5 epoch | 0.209793 | 0.350628 | 0.410680 | 0.427938 | 0.518071 | 0.858980 | 0.207539 | 9.52 |

3 epoch的IC@6比1轮下降1.53个百分点；5 epoch比1轮下降3.95个百分点，比未训练生成下降1.94个百分点。源dev预选3轮并未带来外部覆盖收益，不能事后改称预选1轮。301道null空选择率仍为0。这说明当前数据／配置下增加epoch没有改善预算内覆盖，不是原SetR或所有训练路线无效的证明。上述Hit／全gold是模型整个输出集合口径，不是Top-6指标；选择数减少与覆盖下降同现，但不据此认定原因。

## 代码与复跑

- `benchmarks/prepare_setr_data.py`：原始外部测试数据与 BM25 Top-20 冻结。
- `benchmarks/prepare_setr_training.py`：源问题去重／连通分量隔离，丢弃旧回复。
- `benchmarks/setr_protocol.py`：prompt、严格 parser、去重 evidence union。
- `benchmarks/run_setr_generation.py`：整池生成、输出指纹、可恢复 JSONL。
- `benchmarks/train_setr_adapter.py`：仅目标 token 的生成式 LoRA，固定训练预算、干净基础模型重载验证。
- `benchmarks/evaluate_setr.py`：同池基线、预测输入核对、gold 只评测。
- `benchmarks/verify_setr_model.py`：只读分片检查与流式 SHA256，manifest 不能写回共享权重目录。
- `benchmarks/audit_setr_teacher.py`：完整源预测／输入指纹校验与分 split 语法门禁，不作 gold 质量判定。
- `benchmarks/run_setr_experiment.py`：仅允许两个专用 SetR runtime 名称，显式传独立教师解释器；0/1 卡教师、2 卡 reranker，结构门禁通过后才训练学生与做完整外部测试。

本地数据命令（输出拒绝覆盖，复跑使用新目录）：

```powershell
$py = 'D:\miniconda3\envs\deepagents\python.exe'
& $py -m pip install ftfy==6.3.1
& $py benchmarks/prepare_setr_data.py --raw-dir output/setr-v1/raw --output output/setr-v1/multihoprag-bm25-v1.json
& $py benchmarks/prepare_setr_training.py --source output/setr-v1/raw/rank_zephyr_training_data-parsed-lfs.jsonl --output output/setr-v1/source-train-dev-v1.json
& $py benchmarks/evaluate_setr.py --dataset output/setr-v1/multihoprag-bm25-v1.json --output output/setr-v1/bm25-baseline-v1.json
& $py -m unittest tests.test_setr_protocol tests.test_hybrid_retrieval
```

远端继续使用 `/home/researcher/workspace/research-assistant-zotero-github/runtime/setr-qwen32b-20261110`（名称保留历史，不代表现用 32B）。教师为项目 `.setr_teacher_env/bin/python`，固定 `transformers==5.3.0`、`huggingface-hub==1.6.0`、`tokenizers==0.22.2`，只读引用 selector／GPU 基础依赖，未改共享包。学生／评测仍用 `.selector_env/bin/python`。教师不运行继承的 vLLM（该包要求 transformers<5；pip 提示这项冲突已记录，不应用此教师环境启动 vLLM）。环境 freeze、版本记录及 smoke 日志位于该 runtime 的 `output/`。当前无 FLA／causal-conv1d 加速，原生 Torch 回退可能较慢，先实测、不预报吞吐。原始数据与模型不入 Git。

## 失败记录

- 学生 LoRA 两次训练均完成 512 条 train／64 个优化步，loss 正常并写出 adapter，但原 fresh-reload 门禁失败。只读诊断显示 adapter reload 重复推理差异为 `0`、输出有限；训练时 `use_cache=False`，reload 默认 `use_cache=True`，BF16＋SDPA 的 logits 最大差异为 `0.4375`，超过原 `atol=0.02, rtol=0.01`，属于校验执行模式不一致，不是 adapter 损坏或训练过程失败。已在 reload 模型显式设置 `fresh.config.use_cache=False`，两次失败目录和日志分别保留为 `student-adapter-qwen35-v1-failed-reload-diagnostic[-2]`；第三次同配置重跑中，只有 fresh reload 通过才进入正式评测。

- 未先查共享资源便下载：初查仅覆盖项目 `models/` 和默认 HF cache，不能据此认定服务器没有模型。用户指出后先 SIGSTOP，再核对 PID 836170 的 Python 命令与本轮目录内 `.lock`／`.incomplete` 文件句柄，定向结束下载；部分文件保留，共享目录不改。补查当前 `research-eval` 的 `/storage/models`（受限深度配置扫描）、`/data1/llm`、`/home/allon/models` 等，发现共享 Qwen3.5-27B、Qwen3-30B-A3B-Thinking-2507、Qwen3.5-35B-A3B，尚未定位 Qwen-32B；这不证明其他服务器或路径不存在。等待用户确认所指模型路径／服务器，不能把近似型号冒充 32B。

- 首次数据构建被 120 秒超时中断：改为无总时限后台任务，完成冻结文件，未将中断产物当完整数据。
- 首次远端启动在创建日志时失败（不存在 `output/`）：没有启动训练；修正为 Python 创建专用目录后用绝对解释器执行，并由 SSH 前台子进程回传退出码。仍需等待完整任务结果确认模型执行和质量。
- 官方配对数据核查：完整文件树／5 次提交历史审计固定于 `d1ac78b7e41f7c6a2cd8d582934fbba02893f343`；仓库 `train_data/` 只含 `rank_zephyr_training_data-parsed.jsonl`；README 明确其 `conversations` 继承 RankZephyr，要求重新运行 `generate_data.py`。仓库无 release，[issue #1](https://github.com/LGAI-Research/SetR/issues/1) 仍是请求发布 SETR 模型，未提供 checkpoint 或 GPT-4o responses；HF 按 `SetR` 和 `arxiv:2507.06838` 搜索、LG 组织数据集列表均未找到可确认为官方的配对。准确结论是：**在已检查的公开入口中未找到 SetR GPT-4o 配对文件**，不是证明作者没有私有数据。
- Qwen-4B 教师曾启动并产生部分日志，但不是本次正式标签来源；进程已停止，部分输出保留为失败／替代实验记录，不用于本次训练。
- Qwen3-32B 下载与后续运行改放 `runtime/setr-qwen32b-20261110`，旧运行目录保留。单个教师进程使用可见 GPU 0/1，BF16 不量化，每卡权重分派上限 40 GiB，拒绝 CPU／磁盘 offload；保留第 3 张卡和生产服务所在第 4 张卡的隔离。下载成功、权重哈希验证、实际模型加载和首批合法标签是四个不同状态，不提前宣称生成完成。
