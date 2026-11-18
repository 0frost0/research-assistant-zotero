"""Freeze a broader, source-checked development set before scoring.

Uses only the existing six-paper SQLite snapshot. Extracts exact anchors from
stored pages (not retrieval outputs); never modifies production data. Short
locator strings below are not independent human labels. No answer-model calls.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3


# Six distinct information needs per paper, no synonym copies to inflate n.
# paper prefix, page, category, language, query, exact whitespace-normalized anchor
SPECS = [
    ('6876484c', 1, 'method', 'zh', 'TCF 论文的 Pathology-Focused Binning 优先为哪些数值范围分配精度？', 'prioritizes clinically significant numerical ranges over usual values'),
    ('6876484c', 1, 'method', 'zh', 'TCF 的 Dual-Calendar RoPE 联合编码哪两类时间信号？', 'encodes absolute and relative temporal signals'),
    ('6876484c', 1, 'result', 'en', 'In Time-Conditioned Foreseeing, what is the reported maximum AUPRC improvement across downstream tasks?', 'achieving up to a 54% improvement in AUPRC'),
    ('6876484c', 4, 'definition', 'zh', 'TCF 如何用三元组表示患者历史中的一个事件？', 'denoting the timestamp, feature, and value, respectively'),
    ('6876484c', 19, 'number', 'zh', 'TCF foresee 目标一次提供多少个未来时间戳？', 'number of future timestamps provided for the foresee objective, set to 10'),
    ('6876484c', 25, 'number', 'en', 'How many decoder layers and attention heads does the TCF backbone use?', 'Number of Decoder Layers (N): 6 •Number of Attention Heads: 8'),
    ('22c2a8e5', 1, 'dataset', 'zh', 'RePrompT 使用哪两个 MIMIC 数据集进行实验？', 'Experiments on MIMIC-III and MIMIC-IV'),
    ('22c2a8e5', 4, 'method', 'en', 'In RePrompT prompt tuning, are the internal LLM weights updated or only the prompts optimized?', 'Because only the prompt parameters are optimized'),
    ('22c2a8e5', 5, 'method', 'zh', 'RePrompT 选择哪个结构化 EHR 编码器产生患者表征？', 'we adopt RETAIN'),
    ('22c2a8e5', 6, 'condition', 'zh', 'RePrompT 为什么排除只有一次就诊记录的患者？', 'Since these task requires longitudinal data, we excluded patients with only a single recorded visit'),
    ('22c2a8e5', 6, 'number', 'zh', 'RePrompT 实验如何划分训练测试集，结果平均多少次运行？', '70% training and 30% testing sets and report the mean over three runs'),
    ('22c2a8e5', 13, 'condition', 'en', 'Does RePrompT group medications by ATC classes in medication recommendation, and why?', 'we do not group medications according to the ATC ontology, as our goal is to directly recommend specific medications rather than medication classes'),
    ('ff783f99', 1, 'number', 'zh', 'Dynamic-DeepHit 囊性纤维化数据集包含多少名成年患者？', '5883 adult patients'),
    ('ff783f99', 1, 'dataset', 'en', 'Which registry supplies the Dynamic-DeepHit cohort and what years do its annual follow-ups span?', 'U.K. Cystic Fibrosis Registry, which includes a heterogeneous cohort of 5883 adult patients with annual follow-ups between 2009 to 2015'),
    ('ff783f99', 7, 'condition', 'zh', 'Dynamic-DeepHit 的囊性纤维化实验对患者年龄的纳入条件是什么？', 'included only patients who were aged 18 years or older'),
    ('ff783f99', 7, 'number', 'zh', 'Dynamic-DeepHit 的 CF 实验共有多少个静态和时变协变量？', '90 features (11 static covariates and 79 time-varying covariates)'),
    ('ff783f99', 7, 'condition', 'zh', 'Dynamic-DeepHit 将时间离散到什么粒度，为什么？', 'resolution of one month since the date information in the data was mostly available in month format'),
    ('ff783f99', 11, 'method', 'en', 'When can Dynamic-DeepHit update risk predictions, compared with predefined landmarking times?', 'whenever a new observation is made for that subject'),
    ('36f309e4', 1, 'result', 'zh', '不规则多模态 EHR 论文报告的时间序列、临床笔记和融合 F1 相对提升各是多少？', 'relative improvements of 6.5%, 3.6%, and 4.3% in F1 for time series, clinical notes, and multimodal fusion, respectively'),
    ('36f309e4', 3, 'method', 'zh', 'UTDE 的门控机制融合哪两种时间序列嵌入？', 'dynamically fusing embeddings of imputation and a time attention module'),
    ('36f309e4', 5, 'method', 'en', 'At which three levels can the UTDE gating mechanism operate?', 'patient level'),
    ('36f309e4', 6, 'dataset', 'zh', 'UTDE 不规则多模态 EHR 实验的两个任务及各自预测窗口是什么？', '48-hour in-hospital mortality prediction (48-IHM) and 24-hour phenotype classification (24-PHE)'),
    ('36f309e4', 6, 'condition', 'zh', 'UTDE 多模态实验如何处理预测时间之前完全没有临床笔记的患者？', 'patients without any clinical notes before the prediction time are removed'),
    ('36f309e4', 12, 'condition', 'en', 'How many clinical notes are retained for patients with more than five notes in the UTDE paper, and why?', 'we utilize the last 5 clinical notes preceding the prediction time, due to computational resource limitations'),
    ('b26fc4ad', 1, 'number', 'zh', 'MOTOR 发布的预训练模型有多少参数？', '143M parameter pretrained model'),
    ('b26fc4ad', 1, 'definition', 'en', 'What does MOTOR stand for?', 'Many Outcome Time Oriented Representations'),
    ('b26fc4ad', 1, 'number', 'zh', 'MOTOR 预训练最多使用多少患者记录和临床事件？', '55M patient records (9B clinical events)'),
    ('b26fc4ad', 1, 'dataset', 'zh', 'MOTOR 的迁移学习在多少任务、多少患者数据库上评测？', '19 tasks, across 3 patient databases'),
    ('b26fc4ad', 3, 'number', 'en', 'How many tasks does MOTOR use for self-supervised pretraining?', '8,192 tasks'),
    ('b26fc4ad', 5, 'method', 'zh', 'MOTOR 适配目标任务时使用哪两种策略？', 'a linear probe (MOTOR-Probe) and full finetuning (MOTOR-Finetune)'),
    ('9246ea14', 1, 'number', 'zh', 'TRIPOD+AI报告清单包含多少项？', 'expanded 27 item checklist'),
    ('9246ea14', 1, 'condition', 'zh', 'TRIPOD+AI 发布后，2015 版清单是否还应继续使用？', 'The new checklist supersedes the TRIPOD 2015 checklist, which should no longer be used'),
    ('9246ea14', 2, 'number', 'en', 'How many items does the original TRIPOD 2015 checklist contain, not TRIPOD+AI?', 'TRIPOD 2015 comprises a checklist of 37 items'),
    ('9246ea14', 3, 'method', 'zh', 'TRIPOD+AI 清单采用了哪两类主要开发方法？', 'Literature reviews and consensus exercises were used to develop the TRIPOD+AI checklist'),
    ('9246ea14', 4, 'definition', 'en', 'In the TRIPOD+AI glossary, what is calibration?', 'Agreement between observed outcomes and estimated values from the model'),
    ('9246ea14', 4, 'condition', 'zh', 'TRIPOD+AI 对评估数据和训练、超参数调整、模型选择数据的关系有什么要求？', 'Evaluation data should be distinct from the data used to train the model, tune hyperparameters, or do model selection'),
]


def extract_anchor(text, locator):
    pattern = r'\s*'.join(re.escape(c) for c in re.sub(r'\s+', '', locator))
    match = re.search(pattern, text)
    if match is None:
        raise ValueError('Anchor not present: ' + locator)
    return text[match.start():match.end()]


def build(root, output):
    path = (root / '.data/zotero/knowledge.sqlite3').resolve()
    con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        papers = {sha: json.loads(pages) for sha, pages in con.execute('SELECT hash,pages FROM objects')}
    finally:
        con.close()
    kb = '811dcbd3-900a-5290-8c72-b308a731d5ca'
    cases = []
    for i, (prefix, page, category, language, query, locator) in enumerate(SPECS, 1):
        sources = [sha for sha in papers if sha.startswith(prefix)]
        if len(sources) != 1:
            raise ValueError('Expected one local paper for ' + prefix)
        source = sources[0]
        text = next(p['text'] for p in papers[source] if p['page'] == page)
        quote = extract_anchor(text, locator)
        if prefix == '36f309e4' and page == 5:
            # The complete three-level answer includes formula fragments; do
            # not label a single word as evidence for all three levels.
            start = text.index(quote)
            last = extract_anchor(text[start:], 'hidden space level')
            end = text.index(last, start) + len(last)
            quote = text[start:end]
        cases.append({'id': f'X{i:02}', 'query': query, 'kb_ids': [kb], 'scope': 'original',
                      'no_answer': False, 'paper': prefix, 'category': category, 'language': language,
                      'expected': [{'source_id': source + '.pdf', 'page': page, 'quote': quote}]})
    for i, query in enumerate(['什么是心衰', '这些论文对量子计算容错门阈值的实验结论是什么？',
                               'RePrompT 的阿尔茨海默病随机双盲临床试验给药剂量是多少？',
                               'What is the recommended clinical drug dose for the MOTOR model?'], 1):
        cases.append({'id': f'N{i:02}', 'query': query, 'kb_ids': [kb], 'scope': 'original',
                      'no_answer': True, 'category': 'no_answer', 'language': 'en' if i == 4 else 'zh',
                      'expected': []})
    dataset = {'id': 'qwen-stage1-library-expanded-40-v1', 'split': 'development',
               'provenance': 'assistant_source_checked_before_scoring_not_independent_human_gold',
               'protocol': '36 distinct information needs, six per paper; four unscored no-answer probes; '
                           'all six papers compete in every retrieval; no tuning on this run',
               'coverage': {'papers': 6, 'positive_questions': 36, 'no_answer_questions': 4,
                            'categories': dict(Counter(c['category'] for c in cases)),
                            'languages': dict(Counter(c['language'] for c in cases))},
               'cases': cases}
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError('Frozen dataset already exists; use a new explicit output')
    raw = json.dumps(dataset, ensure_ascii=False, indent=2).encode('utf-8')
    output.write_bytes(raw)
    print(json.dumps({'cases': len(cases), 'sha256': hashlib.sha256(raw).hexdigest(),
                      'coverage': dataset['coverage']}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.root, args.output)
