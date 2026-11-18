"""Freeze source-checked expanded-Zotero development questions, without retrieval.

Read-only SQLite access; no model, vector store, PDF parsing or network calls.
Locators are assistant-authored development labels, not independent human gold.
The short extracted evidence is private: keep the frozen JSON under output/.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.build_qwen_stage1_library_set import extract_anchor


BASE_ID = '2927d58a-490a-54d6-b491-e4723a710efa'
BASE_REVISION = 1
DATASET_ID = 'qwen-stage1-zotero-multitopic-52-v1'
PAPERS = {
    'f5acfae2': ('AgentMD', 'medical_agents'),
    'e3195727': ('MedRAX', 'medical_agents'),
    '578cd834': ('LINK-HF', 'heart_failure_clinical'),
    '51c624d3': ('MONITOR-HF', 'heart_failure_clinical'),
    'c5839fcf': ('Blinded AI LVEF trial', 'heart_failure_clinical'),
    'e2a0ff81': ('CARER', 'temporal_ehr'),
    '1b1d83b1': ('FedProto', 'federated_learning'),
    'cab623e2': ('Harmony', 'federated_learning'),
    '7a0b2746': ('RED-CNN', 'medical_image_restoration'),
    'ee87e299': ('Restormer', 'image_restoration'),
    'c27e338e': ('SDEdit', 'generative_models'),
    'da4b3fd7': ('StyleGAN', 'generative_models'),
}

# Four different information needs per paper, fixed before inspecting any ranks.
# prefix, category, language, question, [(physical PDF page, exact locator), ...]
SPECS = [
    ('f5acfae2', 'number', 'zh', 'AgentMD 的 RiskCalcs 工具库包含多少个可执行临床计算器？', [(1, '2,164 executable clinical calculators')]),
    ('f5acfae2', 'method', 'zh', 'AgentMD 在 RiskQA 中先用哪个检索模型取回多少个工具，再让语言模型选择？', [(3, 'AgentMD selects the most suitable tool from the top 10 tools returned by MedCPT')]),
    ('f5acfae2', 'result', 'en', 'What pass rates does AgentMD report for automatically generated unit-test parameters versus the manually curated challenging subset?', [(3, '91.6% of the AgentMD calculations are consistent with manual calculations'), (3, 'For this manually curated subset, we speciﬁcally included more challenging and edge cases where the patient parameters fall close to the decision boundaries. As a result, AgentMD achieves a passing rate of 84.0%')]),
    ('f5acfae2', 'condition', 'zh', 'AgentMD 创建计算器时为什么只用 PubMed 摘要，这会漏掉什么信息？', [(6, 'the creation of calculator tools was restricted to PubMed abstracts, overlooking detailed descriptions in full-text articles due to accessibility issues')]),

    ('e3195727', 'dataset', 'zh', 'MedRAX 的 ChestAgentBench 有多少道查询、多少类能力，来自多少个专家整理病例？', [(2, '2,500 complex medical queries across 7 categories, built from 675 expert-curated clinical cases')]),
    ('e3195727', 'method', 'zh', 'MedRAX 使用什么推理与行动循环，短期记忆保存哪些内容？', [(3, 'MedRAX employs a LLM as the core to drive a ReAct (Reasoning and Acting) loop'), (3, 'the system maintains a short-term memory of user interactions, tool outputs, and images to support multi-turn interactions')]),
    ('e3195727', 'condition', 'en', 'In MedRAX evaluation, how many retries are allowed for invalid responses, errors or timeouts, and how are still-invalid responses scored?', [(6, 'For unclear responses, errors, or timeouts, we retry up to three times. Responses that remain invalid or do not choose a single choice are marked incorrect')]),
    ('e3195727', 'implementation', 'zh', 'MedRAX 论文实验使用哪个骨干语言模型，部署在什么 GPU 配置上？', [(6, 'MedRAX uses GPT-4o as its backbone LLM and is deployed on a single NVIDIA RTX 6000 GPU')]),

    ('578cd834', 'condition', 'zh', 'LINK-HF 研究对成年患者的心功能分级和本次住院原因有什么纳入要求？', [(2, 'Adult subjects (≥18 years) with a history of HF and New York Heart Association functional class II-IV symptoms who were hospitalized for acute HF exacerbation were eligible for study participation')]),
    ('578cd834', 'method', 'zh', 'LINK-HF 的贴片数据如何从传感器传到手机和云端，云端上传是否加密？', [(3, 'The sensor patch was paired via bluetooth with an android phone equipped with a conventional cellular data plan, and data from the sensors were continuously streamed to the phone'), (3, 'Data collected on the phone were encrypted and uploaded through cellular connectivity at configurable intervals to a cloud analytics platform')]),
    ('578cd834', 'condition', 'en', 'Were care providers informed of the clinical alerts in LINK-HF, and was this an interventional or observational study?', [(4, 'In this observational study, providers were not made aware of clinical alerts')]),
    ('578cd834', 'result', 'zh', 'LINK-HF 有多少个住院事件因依从性导致数据不足，分别如何处理 ROC 和时间至事件分析？', [(6, 'Sensor use compliance failure by the subject led to 5 events with insufficient data. These 5 events were excluded from the ROC analysis but are included in the time-toevent analyses')]),

    ('51c624d3', 'dataset', 'zh', 'MONITOR-HF 在哪个国家、多少中心开展，采用开放标签还是盲法设计？', [(4, 'MONITOR-HF was a prospective multicentre (25 hospitals) open-label randomised clinical trial done in the Netherlands')]),
    ('51c624d3', 'condition', 'zh', 'MONITOR-HF 对 NYHA 分级、过去 12 个月失代偿就医和射血分数有哪些入组条件？', [(4, 'The MONITOR-HF trial enrolled patients with NYHA class III chronic heart failure with a previous hospital admission for decompensated heart failure or urgent visit with the necessity of intravenous diuretics in the past 12 months, irrespective of left ventricular ejection fraction')]),
    ('51c624d3', 'definition', 'en', 'What was the primary efficacy endpoint of MONITOR-HF, including the questionnaire and assessment time?', [(4, 'The primary efficacy endpoint was the mean change in Kansas City Cardiomyopathy Questionnaire (KCCQ) overall summary scores from baseline to 12 months between groups')]),
    ('51c624d3', 'result', 'zh', 'MONITOR-HF 随机分配了多少名患者，12 个月 KCCQ 总体评分变化的组间差及 95% 置信区间是多少？', [(2, 'we randomly assigned 348 patients'), (2, 'The difference in mean change in KCCQ overall summary score at 12 months was 7·13 (95% CI 1·51–12·75; p=0·013) between groups')]),

    ('c5839fcf', 'definition', 'zh', '超声技师与 AI 的盲法 LVEF 试验如何定义初始与最终评估之间的 substantial change？', [(6, 'Substantial change was defined as greater than 5% change in LVEF between initial and final assessments')]),
    ('c5839fcf', 'result', 'zh', '盲法 LVEF 试验中，AI 组和超声技师组初始到最终评估出现显著修改的研究数和比例各是多少？', [(2, 'The primary outcome of substantial change between the initial and final assessments occurred in 292 (16.8%) studies in the AI group compared with 478 (27.2%) studies in the sonographer group')]),
    ('c5839fcf', 'condition', 'en', 'In the blinded LVEF trial, where was the AI trained, how many videos were used, and had it seen data from the trial centre?', [(6, 'The AI model was trained using 144,184 echocardiogram videos from Stanford Healthcare and was never exposed to videos from the Cedars-Sinai Medical Center')]),
    ('c5839fcf', 'limitation', 'zh', '盲法 LVEF 临床试验在中心数量和长期结局评估能力上有哪些明确限制？', [(4, 'our study was single centre, reflecting the demographics and clinical practices of a particular population'), (4, 'the study was not powered to assess long-term outcomes based on differences in LVEF assessment')]),

    ('e2a0ff81', 'dataset', 'zh', 'CARER 从 PrimeKG 选用了多少疾病节点和多少文本节点特征构建医学语料？', [(4, 'we only utilize 17,080 disease nodes in the knowledge graph'), (4, 'In total, there are 14,252 textual node features, all of which we use to construct our final medical corpus')]),
    ('e2a0ff81', 'condition', 'zh', 'CARER 为什么在 MIMIC-IV 中排除临床笔记，却在 MIMIC-III 使用全部四类模态？', [(6, 'For MIMIC-III, we utilize all four data modalities. For MIMIC-IV, we exclude the clinical notes (N) because they are all “Discharge summaries” which typically contain direct indications of the diagnoses')]),
    ('e2a0ff81', 'method', 'en', 'Which two views does CARER align: what supplies the local view and what supplies the global view?', [(5, 'the multimodal featureszE captures the local patterns and relationships presented in the patient’s local data, the clinical reasoningzR provides a more “global” view obtained from external documents and LLM’s rationales')]),
    ('e2a0ff81', 'result', 'zh', 'CARER 正文报告的全诊断预测与心衰预测相对最佳基线的提升范围各是多少？', [(6, 'CARER achieves a performance gain of around 4-11.2% against the best baseline MedMHP for the report metrics for Full Diagnosis Task, while those margins for Heart Failure Prediction Tasks tend to be less significant (0.96-5%)')]),

    ('1b1d83b1', 'method', 'zh', 'FedProto 的客户端与服务器交换的是什么，而不是梯度？', [(1, 'the clients and server communicate the abstract class prototypes instead of the gradients')]),
    ('1b1d83b1', 'definition', 'zh', 'FedProto 中，一个客户端某个类别的 prototype 是如何由该类样本表示得到的？', [(3, 'For the i-th client, the prototype is the mean value of the embedding vectors of instances in classj')]),
    ('1b1d83b1', 'condition', 'en', 'Does FedProto require clients to have identical model architectures and identical class sets?', [(4, 'Using prototypes in FL, we do not need to exchange gradients or model parameters, which means that the proposed solution can tackle heterogeneous model architectures. Moreover, the prototype-based FL does not require each client to provide the same classes')]),
    ('1b1d83b1', 'implementation', 'zh', 'FedProto 实验一共有多少客户端，每轮通信采样其中多少客户端？', [(6, 'We use 20 clients for all datasets and all clients are sampled in each communication round')]),

    ('cab623e2', 'method', 'zh', 'Harmony 将多模态联邦训练拆成哪两个阶段，第二阶段由哪些节点协作训练分类器？', [(5, 'a novel two-stage framework, namely modality-wise federated learning and federated fusion learning'), (5, 'In federated fusion learning, only multi-modal nodes will collaboratively train the classifier layers')]),
    ('cab623e2', 'dataset', 'zh', 'Harmony 的真实家庭测试床部署了多少个节点、持续多久，使用哪三类传感器？', [(2, 'We deployed Harmony on a real-world multi-modal sensor testbed for four continuous weeks, which consists of 16 nodes installed in the homes of elderly subjects'), (2, 'three types of sensors (depth cameras, mmWave radars, and microphones)')]),
    ('cab623e2', 'condition', 'en', 'At inference time, how does Harmony adapt its model choice when the available sensor modalities change?', [(5, 'the nodes can select either multi-modal or single-modal models during inference, according to the modality of their local data over time. This feature allows Harmony to adapt to runtime dynamics such as sensor failures')]),
    ('cab623e2', 'implementation', 'zh', 'Harmony 如何分配多模态节点的计算资源来平衡不同单模态 FL 子系统的训练延迟？', [(5, 'the multi-modal nodes will allocate more computing resources to the modalities whose unimodal FL subsystems take longer to aggregate models in a single training round')]),

    ('7a0b2746', 'method', 'zh', 'RED-CNN 为什么在编码器里去掉池化层？', [(3, 'since the pooling layer (downsampling) after a convolutional layer may discard important structural details, it is abandoned in our encoder')]),
    ('7a0b2746', 'condition', 'zh', 'RED-CNN 的模拟 CT 实验如何避免训练集和测试集包含同一患者的图像？', [(5, 'Images from the patients in the training set were not in the testing set')]),
    ('7a0b2746', 'dataset', 'en', 'How many patients and what dose pairs does RED-CNN use in its Mayo clinical dataset, and what patient-wise cross-validation protocol is used?', [(5, 'The dataset contained 2,378 3mm thickness full and quarter dose 512 × 512 CT images from 10 patients'), (5, 'While testing on CT images from each patient, the images from the other 9 patients were involved in the training phase')]),
    ('7a0b2746', 'result', 'zh', 'RED-CNN 在胸部示例的四个 ROI 定量比较中，RMSE、PSNR 和 SSIM 相对其他方法的表现如何？', [(6, 'For quantitative evaluation, four ROIs were chosen as highlighted by red dotted boxes in Fig. 4(a). The results are in Fig. 7. The quantitative results followed similar trends per visual inspection. The RED-CNN had the lowest RMSE and the highest PSNR/SSIM for all the ROIs')]),

    ('ee87e299', 'method', 'zh', 'Restormer 的 MDTA 是沿空间位置还是特征通道计算注意力，使用什么统计关系形成注意力图？', [(2, 'It applies SA across feature dimension rather than the spatial dimension, i.e., instead of explicitly modeling pairwise pixel interactions, MDTA computes cross-covariance across feature channels to obtain attention map')]),
    ('ee87e299', 'implementation', 'zh', 'Restormer 渐进训练中，随着 epoch 增加，图像 patch 和 batch 大小分别如何变化？', [(2, 'the network is trained on small patches and large batches in early epochs, and on gradually large image patches and small batches in later epochs')]),
    ('ee87e299', 'result', 'en', 'For Restormer deraining, what is the average PSNR gain over SPAIR across the five datasets?', [(5, 'Compared to the recent best method SPAIR [61], Restormer achieves 1.05 dB improvement when averaged across all datasets')]),
    ('ee87e299', 'condition', 'zh', 'Restormer 的运动去模糊模型仅在哪个数据集训练，又直接应用到哪些外部基准？', [(5, 'Our Restormer is trained only on the GoPro dataset [56] and directly applied to the HIDE [67] and RealBlur [65] benchmark datasets')]),

    ('c27e338e', 'method', 'zh', 'SDEdit 对 RGB 用户引导图先做什么，再通过什么过程提高真实感？', [(1, 'SDEdit ﬁrst adds noise to the input, then subsequently denoises the resulting image through the SDE prior to increase its realism')]),
    ('c27e338e', 'condition', 'zh', 'SDEdit 应用于新编辑任务时，是否要求任务专属训练或 GAN inversion？', [(1, 'SDEdit does not require task-speciﬁc training or inversions')]),
    ('c27e338e', 'result', 'en', 'In the LSUN stroke-generation experiment, what happens to realism and faithfulness as SDEdit t0 increases?', [(4, 'As t0 increases, the generated images become more realistic while less faithful')]),
    ('c27e338e', 'implementation', 'zh', 'SDEdit 交互编辑如何初始化 t0，再根据用户偏好调整这个超参数？', [(5, 'In interactive settings (where user draws a sketch-based guide), we can initialize t0∈ [0.3, 0.6], synthesize a candidate with SDEdit, and ask the user whether the sample should be more faithful or more realistic; from the responses, we can obtain a reasonable t0 via binary search')]),

    ('da4b3fd7', 'implementation', 'zh', '原版 StyleGAN 的映射网络有几层，输入与中间潜空间的维数设为多少？', [(2, 'spaces to 512, and the mapping f is implemented using an 8-layer MLP')]),
    ('da4b3fd7', 'method', 'zh', '原版 StyleGAN 的逐层噪声输入是什么分布，以什么缩放方式加到卷积输出？', [(2, 'These are single-channel images consisting of uncorrelated Gaussian noise, and we feed a dedicated noise image to each layer of the synthesis network. The noise image is broadcasted to all feature maps using learned perfeature scaling factors and then added to the output of the corresponding convolution')]),
    ('da4b3fd7', 'condition', 'en', 'Did the original StyleGAN use the truncation trick to compute the reported FIDs, or only for illustrations?', [(3, 'All FIDs in this paper are computed without the truncation trick, and we only use it for illustrative purposes in Figure 2 and the video')]),
    ('da4b3fd7', 'definition', 'zh', '原版 StyleGAN 提出哪两个自动指标来量化潜空间插值质量与解耦？', [(1, 'two new automated metrics — perceptual path length and linear separability — for quantifying these aspects of the generator')]),
]

PROBES = [
    ('zh', 'MedRAX 论文报告的阿尔茨海默病药物随机双盲试验剂量是多少？', 'false_study_premise'),
    ('zh', 'SDEdit 用于心衰治疗时推荐的临床给药剂量是多少？', 'method_to_drug_category_error'),
    ('en', 'What quantum error-correction threshold did the LINK-HF study experimentally measure?', 'out_of_domain_premise'),
    ('zh', '我昨天在未同步的纸质笔记中为哪篇论文打了几分？', 'unavailable_private_context'),
]


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def construct(root):
    """Construct in memory; one consistent read transaction, no production writes."""
    path = (root / '.data/zotero/knowledge.sqlite3').resolve()
    con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        con.execute('BEGIN')
        base = con.execute('SELECT revision,snapshot_hash,archived FROM knowledge_bases WHERE id=?', (BASE_ID,)).fetchone()
        if base is None or base[2] or base[0] != BASE_REVISION:
            raise ValueError('Expected active frozen base revision 1')
        rows = con.execute('SELECT e.id,e.revision,e.payload,o.hash,o.pages FROM entities e '
                           'JOIN memberships m ON m.entity_id=e.id '
                           "JOIN objects o ON o.hash=json_extract(e.payload,'$.hash') "
                           "WHERE m.kb_id=? AND e.kind='document' ORDER BY e.id", (BASE_ID,)).fetchall()
    finally:
        con.close()
    papers = {row[3]: json.loads(row[4]) for row in rows}
    manifest = [{'entity_id': ident, 'revision': revision, 'source_hash': sha,
                 'payload_sha256': digest(payload), 'pages_sha256': digest(pages)}
                for ident, revision, payload, sha, pages in rows]
    cases = []
    for index, (prefix, category, language, query, anchors) in enumerate(SPECS, 1):
        sources = [sha for sha in papers if sha.startswith(prefix)]
        if len(sources) != 1:
            raise ValueError('Expected one in-scope PDF for ' + prefix)
        source = sources[0]
        expected = []
        for page, locator in anchors:
            text = next((p['text'] for p in papers[source] if p['page'] == page), None)
            if text is None:
                raise ValueError(f'Missing physical page {prefix}:{page}')
            quote = extract_anchor(text, locator)
            expected.append({'source_id': source + '.pdf', 'page': page, 'quote': quote})
        name, topic = PAPERS[prefix]
        cases.append({'id': f'Z{index:02}', 'query': query, 'kb_ids': [BASE_ID],
                      'scope': 'original', 'no_answer': False, 'paper': prefix,
                      'paper_name': name, 'topic': topic, 'category': category,
                      'language': language,
                      'evidence_shape': 'multi_anchor' if len(expected) > 1 else 'single_anchor',
                      'anchor_policy': 'all_required_not_exhaustive_relevance_gold',
                      'expected': expected})
    for index, (language, query, reason) in enumerate(PROBES, 1):
        cases.append({'id': f'ZN{index:02}', 'query': query, 'kb_ids': [BASE_ID],
                      'scope': 'original', 'no_answer': True, 'category': 'no_answer',
                      'language': language, 'scored': False, 'probe_reason': reason,
                      'absence_status': 'not_exhaustively_verified_across_corpus', 'expected': []})
    positive = [case for case in cases if not case['no_answer']]
    return {'id': DATASET_ID, 'split': 'development',
            'provenance': 'assistant_source_checked_before_scoring_not_independent_human_gold',
            'protocol': {'stage': 'embedding_reranker_only', 'selection': 'source_pages_before_retrieval_ranks',
                         'scope': 'all_snapshot_papers_compete_not_per_question_paper_filter',
                         'anchors': 'all_listed_anchors_required; not_exhaustive_relevance_judgments',
                         'probes': 'unscored; no verified corpus_wide_absence_or_abstention_accuracy',
                         'no_answer_model_calls': True},
            'snapshot': {'kb_id': BASE_ID, 'revision': base[0], 'snapshot_hash': base[1],
                         'pdf_entities': len(rows), 'unique_pdfs': len(papers),
                         'membership_manifest_sha256': digest(json.dumps(manifest, sort_keys=True)),
                         'manifest': manifest},
            'coverage': {'papers': len({c['paper'] for c in positive}),
                         'positive_questions': len(positive), 'no_answer_questions': len(PROBES),
                         'anchors': sum(len(c['expected']) for c in positive),
                         'topics': dict(Counter(c['topic'] for c in positive)),
                         'categories': dict(Counter(c['category'] for c in positive)),
                         'languages': dict(Counter(c['language'] for c in positive)),
                         'evidence_shapes': dict(Counter(c['evidence_shape'] for c in positive)),
                         'nonfirst_page_questions': sum(all(g['page'] > 1 for g in c['expected']) for c in positive)},
            'cases': cases}


def render_questions(dataset, sha256):
    """Human-readable companion; deliberately excludes extracted source quotes."""
    lines = ['# Zotero 多主题检索开发题集 v1', '',
             f"题集：`{dataset['id']}`；split：**development，非独立人工 test**。", '',
             '48 道正例＋4 道未计分边界探针。每篇 4 个不同信息需求；来源为现有导入页，未查看检索排名选题。',
             '页码是 PDF 物理页，不是期刊印刷页；下列不复制论文摘录，逐字锚点见本机冻结 JSON。', '',
             f'冻结 JSON SHA-256：`{sha256}`', '',
             '单锚点不等于单事实：一段原文可能支持多个要点；多锚点表示需要多个指定证据片段。',
             '所有正例检索范围相同，都是整个快照库；paper 标签不作为检索过滤器。', '']
    for prefix, (name, topic) in PAPERS.items():
        lines.extend([f'## {name} · {topic}', ''])
        for case in dataset['cases']:
            if case.get('paper') != prefix:
                continue
            pages = ', '.join(str(page) for page in sorted({g['page'] for g in case['expected']}))
            lines.append(f"- **{case['id']}** [{case['category']} / {case['language']} / {case['evidence_shape']}] {case['query']}（物理页 {pages}）")
        lines.append('')
    lines.extend(['## 未计分边界探针', '',
                  '这些是预期无支持的挑战提示，未穷举核查全库缺席；不计入命中率或拒答准确率。', ''])
    lines.extend(f"- **{c['id']}** {c['query']}" for c in dataset['cases'] if c['no_answer'])
    return '\n'.join(lines) + '\n'


def build(root, output):
    if output.exists():
        raise FileExistsError('Frozen dataset already exists; use a new explicit output')
    dataset = construct(root)
    raw = json.dumps(dataset, ensure_ascii=False, indent=2).encode('utf-8')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as stream:
        stream.write(raw)
    report = {'dataset_id': dataset['id'], 'sha256': hashlib.sha256(raw).hexdigest(),
              'coverage': dataset['coverage']}
    print(json.dumps(report, ensure_ascii=False))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--questions', type=Path, help='Optional new Markdown companion without quotes')
    args = parser.parse_args()
    if args.questions and args.questions.exists():
        raise FileExistsError('Question companion already exists')
    report = build(args.root, args.output)
    if args.questions:
        args.questions.parent.mkdir(parents=True, exist_ok=True)
        with args.questions.open('x', encoding='utf-8') as stream:
            stream.write(render_questions(json.loads(args.output.read_bytes()), report['sha256']))
