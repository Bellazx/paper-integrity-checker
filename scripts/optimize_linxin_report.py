#!/usr/bin/env python3
"""Normalize the one Lin Xin report after independent Codex recheck."""
import json
import subprocess
from pathlib import Path
from PIL import Image, ImageOps

BASE = Path('/home/sjtu/paper-integrity-checker')
REPORT = BASE / 'data/output/yujing_scholar/init/10.1145_3784833.3784893/report.json'
JUDGE = BASE / 'data/yujing_scholar_linxin_round_1/results_judge/batch_2301040.json'

report = json.loads(REPORT.read_text(encoding='utf-8'))
paper = report.setdefault('paper', {})
paper.update({
    'author': 'Xin Lin',
    'authors_full': ['Xin Lin', 'Lei Yan', 'Jing Liu', 'Rui Peng', 'Qiang Fu', 'Tao Yi'],
    'journal': '2025 The 11th International Conference on Communication and Information Processing (ICCIP 2025)',
    'total_references': 10,
})
REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

items = json.loads(JUDGE.read_text(encoding='utf-8'))
item = items[0]
item['ref_review'] = (
    '参考文献维度复核结论：论文参考文献表按双栏顺序完整列出[1]—[10]，初审报告中“参考文献数3”及[4]—[10]缺失属于双栏文本解析误报，不作为本次风险依据。'
    '问题位置：正文引言中的“This distributed satellite [4] cooperative framework”；摘要后续论述中的“deep, high-precision desert petroleum exploration [5]”；正文引言中的“impulse [10]”。'
    '问题类型：引用条目与正文论断的语义对应不匹配。具体情况：[4]题名为视频位深增强，不能支撑分布式卫星协同框架；[5]题名为跨域推荐，不能支撑沙漠石油探测可行性；[10]题名为LFM信号参数估计，不能直接作为冲激GPR体制的依据。'
    '三篇条目的题名、作者、期刊和页码均可在参考文献表逐条复核。需关注原因：这些引用位于引言的关键论证位置，影响论据链可靠性；[4]、[5]的第一作者与本文通讯作者同为Jing Liu，因此还存在不当自引风险，但仅凭现有材料不能认定主观造假意图。'
    '核查依据：论文PDF正文第1、2页和参考文献表、参考文献[4]、[5]、[10]的完整条目，以及Codex CLI独立复核；建议作者解释或更正引用，不将初审的排版解析误报当作实质问题。'
)
item['dimension_verdicts'] = {
    'image': '建议低风险',
    'data': '建议低风险',
    'ref': '建议高风险',
    'methodology': '建议低风险',
}
evidence_dir = BASE / 'data/yujing_scholar_linxin_round_1/evidence/linxin_3784893_pages'
evidence_dir.mkdir(parents=True, exist_ok=True)
pdf = BASE / 'data/input/scholar/林欣/10.1145_3784833.3784893/3784833.3784893.pdf'
for page in (1, 2, 6):
    target = evidence_dir / f'page_{page}.png'
    full = evidence_dir / f'page_{page}_full.png'
    subprocess.run(['pdftoppm', '-f', str(page), '-l', str(page), '-singlefile', '-png', '-r', '180', str(pdf), str(full.with_suffix(''))], check=True)
    with Image.open(full) as image:
        width, height = image.size
        # Focus the evidence on the column containing the cited passage/list.
        if page == 1:
            box = (width // 2, height // 3, width, height - 80)
        elif page == 2:
            box = (0, 0, width // 2, height // 2)
        else:
            box = (width // 2, 0, width, height // 2)
        cropped = image.crop(box)
        # Remove large white margins so the cited text remains readable in the
        # report's bounded evidence frame.
        mask = ImageOps.grayscale(cropped).point(lambda value: 255 if value < 245 else 0)
        bbox = mask.getbbox()
        if bbox:
            left, top, right, bottom = bbox
            cropped = cropped.crop((max(0, left - 18), max(0, top - 18), min(cropped.width, right + 18), min(cropped.height, bottom + 18)))
        cropped.save(target, optimize=True)
item['evidence_images'] = [
    {'dimension': 'ref', 'path': str(evidence_dir / 'page_1.png'), 'caption': '论文第1页右栏：正文将[4]用于“distributed satellite cooperative framework”，同时将[10]列为impulse GPR依据。'},
    {'dimension': 'ref', 'path': str(evidence_dir / 'page_6.png'), 'caption': '论文第6页参考文献表：双栏完整列出[1]—[10]；[4]为视频增强、[5]为跨域推荐、[10]为LFM信号参数估计。'},
]
item['reason'] = (
    '独立复核确认：初审的参考文献数量和缺失条目是双栏解析误报，论文实际有[1]—[10]共10条参考文献；但正文第1页[4]、第2页[5]和第1页[10]的引用语义与对应条目不匹配，属于可定位的引用可靠性问题。'
    '该问题足以支持参考文献维度的高风险提示，但不等同于已经证明学术不端或主观造假。图像与数据维度未发现足以改变结论的问题：Fig.1—Fig.4趋势与公式及Table 2数值基本一致，Fig.5的重复呈现和约3.3%的小数偏差属于需要作者说明的次要问题。建议作者核对并更正[4]、[5]、[10]的引用来源。'
)
items[0] = item
JUDGE.write_text(json.dumps(items, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
