#!/usr/bin/env python3
"""Summarize the completed nine-paper Dai Dawei batch and archive risk PDFs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys
from urllib.parse import unquote, urlsplit
import zipfile
from zoneinfo import ZoneInfo

import fitz
import requests
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Cm, Pt

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from scripts.export_1027_summary_word import font, link, title_for, NAVY, TEAL
from scripts.run_scholar_1027_review import ROUND, rows, scope

POINTS = {
    '10.1038/s41467-022-33055-9': [
        '两组数值在图与源数据中的归属相反。第6页Fig.4a的az22z5面板中，ddD、DDd组约为3.58、1.04；按源数据Fig. 4工作表第13—30行复算，两组约为1.02、3.56。需要作者确认是作图还是源数据组别登记有误；现有证据不能据此否定整篇主要结论。',
        '两处p值报错，但更正后仍显著。第3页Fig.2d中，W22第一穗的p值写为8.9×10^(-9)，复算约为8.91874×10^(-5)；Mo17第一穗写为0.0074，复算约为0.000749386。分别相差约1万倍和10倍，但均未改变“小于0.05”的判断。',
        '部分标准差与原始粒重不符。源数据Fig. 2工作表X116、X119写为27.6838、26.5259 mg，按对应原始粒重复算为25.1523、23.3925 mg，需核对统计报值。',
    ],
    '10.1534/genetics.117.300602': [
        '同一功能类别的基因数量，图表对不上。第10页Fig.6F列出8类基因共330个，补充Table S1相应区块合计290行；其中“营养贮藏活性”一类，图中写44个，表中仅列10行。需要解释图表的计数口径及清单对应关系。',
        '同一基因编号重复出现，却给出不同统计结果。Table S1的GO:0006520区块中，GRMZM2G005887出现6次，倍数为1.10、1.27、1.27、1.31、1.35、1.37，p值也不同。需说明各行实际对应的分析对象，并提供可区分的标识，才能核对这些结果是否属于同一次基因层面的比较。',
        '百粒重百分比与图示数值不一致。第4页正文称突变体百粒重仅为野生型的18%；Fig.1G中两组柱高约为6.69 g和30.19 g，比值约22%。图上读值为近似值，应以原始称重数据核对并统一文字与图示。',
    ],
}


def verify(row):
    name = unquote(urlsplit(row['review_report_url']).path.rsplit('/', 1)[-1])
    source = BASE / 'data/output/yujing_scholar/review_v2' / name
    content = source.read_bytes()
    response = requests.get(row['review_report_url'], timeout=(10, 45))
    response.raise_for_status()
    assert response.content.startswith(b'%PDF-') and response.content == content
    with fitz.open(source) as pdf:
        text = '\n'.join(page.get_text() for page in pdf)
        expected = '有风险' if row['review_result'] == '高风险' else '未发现风险'
        assert '复核结论：' + expected in text.replace(' ', '')
        if row['review_result'] == '高风险':
            assert sum(len(page.get_images()) for page in pdf) > 0
    return {**row, 'source': str(source), 'sha256': hashlib.sha256(content).hexdigest()}


def add_paper(doc, row, number):
    doi = row['doi']
    doc.add_paragraph(f'{number}. {title_for("戴大伟", doi)}', 'Heading 2')
    p = doc.add_paragraph()
    p.paragraph_format.keep_with_next = True
    font(p.add_run('DOI：' + doi + '    '), 9, color='52657A')
    link(p, '查看复核PDF', row['review_report_url'])
    for index, point in enumerate(POINTS[doi], 1):
        p = doc.add_paragraph()
        p.paragraph_format.keep_together = True
        lead, _, rest = point.partition('。')
        p.add_run(f'（{index}）{lead}。').bold = True
        p.add_run(rest)


def main():
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    manifest = [p for p in scope() if p['scholar'] == '戴大伟']
    database = rows()
    selected = [database[('戴大伟', p['doi'].lower())] for p in manifest]
    assert len(selected) == 9 and all(r['status'] == 2 for r in selected)
    with ThreadPoolExecutor(max_workers=4) as pool:
        verified = list(pool.map(verify, selected))
    high = [r for r in verified if r['review_result'] == '高风险']
    low = [r for r in verified if r['review_result'] == '低风险']
    assert len(high) == 2 and len(low) == 7 and {r['doi'] for r in high} == set(POINTS)
    high.sort(key=lambda r: list(POINTS).index(r['doi']))
    prefix = '农生学院_戴大伟_'
    archive = ROUND / (prefix + '有风险论文复核PDF_' + now.strftime('%Y%m%d'))
    word = ROUND / (prefix + '9篇论文复核情况汇总_' + now.strftime('%Y%m%d') + '.docx')
    zipped = archive.with_suffix('.zip')
    if any(p.exists() for p in (archive, word, zipped)):
        backup = ROUND / ('daidawei_summary_backup_' + now.strftime('%Y%m%d_%H%M%S'))
        backup.mkdir()
        for p in (archive, word, zipped):
            if p.exists():
                shutil.move(str(p), str(backup / p.name))
    archive.mkdir()
    for row in high:
        shutil.copy2(row['source'], archive / Path(row['source']).name)

    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin, section.bottom_margin = Cm(2), Cm(1.9)
    section.left_margin = section.right_margin = Cm(2.2)
    normal = doc.styles['Normal']
    font(normal.font, 10.5)
    normal.paragraph_format.line_spacing = 1.25
    normal.paragraph_format.space_after = Pt(7)
    normal.paragraph_format.widow_control = True
    for name, size, color in [('Title', 22, NAVY), ('Heading 1', 16, NAVY), ('Heading 2', 12, TEAL)]:
        style = doc.styles[name]
        font(style.font, size, True, color)
        style.paragraph_format.space_before = Pt(10)
        style.paragraph_format.space_after = Pt(7)
        style.paragraph_format.keep_with_next = True
    font(section.header.paragraphs[0].add_run('农生学院 · 戴大伟 | 论文复核情况汇总'), 8, color='708090')
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    font(footer.add_run('论文复核汇总 · '), 8, color='708090')
    field = OxmlElement('w:fldSimple'); field.set(qn('w:instr'), 'PAGE'); footer._p.append(field)
    doc.add_paragraph('戴大伟｜9篇论文复核情况汇总', 'Title')
    p = doc.add_paragraph('农生学院    ·    ' + now.strftime('%Y年%m月%d日'))
    font(p.runs[0], 10, color='708090')
    intro = ('本次整理农生学院戴大伟的共计9篇论文，已全部完成复核，其中2篇列为有风险论文，7篇未发现风险。'
             '有风险表示存在需要作者解释、补充材料或更正的具体异常，不等同于已经证明存在数据造假；'
             '未发现风险表示在本次取得的材料和核验范围内，未发现足以判定为有风险的明确证据。')
    doc.add_paragraph(intro)
    table = doc.add_table(rows=1, cols=3)
    for cell, label, count in zip(table.rows[0].cells, ['论文总数', '有风险论文', '未发现风险'], [9, 2, 7]):
        shade = OxmlElement('w:shd'); shade.set(qn('w:fill'), 'EDF3F7'); cell._tc.get_or_add_tcPr().append(shade)
        p = cell.paragraphs[0]; p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        font(p.add_run(f'{count}篇\n'), 18, True, NAVY); font(p.add_run(label), 10, color='52657A')
    doc.add_paragraph('有风险论文（2篇）', 'Heading 1')
    doc.add_paragraph('以下列出主要问题，按重要程度排列；详细核查依据及证据截图见对应复核PDF。')
    add_paper(doc, high[0], 1)
    doc.add_page_break()
    doc.add_paragraph('有风险论文（续）', 'Heading 1')
    add_paper(doc, high[1], 2)
    doc.add_paragraph('上述问题涉及图表对应、统计报值或数据标识，需要作者逐项核对；不能据此直接认定论文全部结论无效。')
    doc.add_page_break()
    doc.add_paragraph('未发现风险论文（7篇）', 'Heading 1')
    doc.add_paragraph('其余7篇均已完成复核，在本次取得的材料及核验范围内未发现风险。')
    for index, row in enumerate(low, 1):
        p = doc.add_paragraph(f'{index}. {title_for("戴大伟", row["doi"])}')
        p.paragraph_format.keep_with_next = True
        p = doc.add_paragraph()
        font(p.add_run('DOI：' + row['doi'] + '    '), 9, color='52657A')
        link(p, '查看复核PDF', row['review_report_url'])
    doc.core_properties.title = '农生学院_戴大伟_9篇论文复核情况汇总'
    doc.core_properties.author = '论文复核'
    doc.save(word)
    check = Document(word)
    text = '\n'.join(p.text for p in check.paragraphs)
    assert intro in text and not any(x in text for x in ['高风险', '低风险', '需要注意', '原报告', '初审报告', '\ufffd'])
    for values in POINTS.values():
        assert all(value in text for value in values)
    links = [r.target_ref for r in check.part.rels.values() if r.reltype == RT.HYPERLINK]
    assert len(links) == 9 and set(links) == {r['review_report_url'] for r in verified}
    assert all(url.startswith('http://') for url in links)
    assert check.styles['Normal'].font.name == 'Microsoft YaHei'
    with zipfile.ZipFile(zipped, 'w', zipfile.ZIP_DEFLATED) as package:
        for pdf in sorted(archive.glob('*.pdf')):
            package.write(pdf, arcname=archive.name + '/' + pdf.name)
    with zipfile.ZipFile(zipped) as package:
        assert package.testzip() is None and len(package.namelist()) == 2
    receipt = {'created_at': now.isoformat(), 'total': 9, 'risk': 2, 'no_risk': 7,
               'word': str(word), 'archive': str(archive), 'zip': str(zipped), 'verified_rows': verified}
    (ROUND / 'current_model_final_two' / 'full_summary_receipt.json').write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({k: v for k, v in receipt.items() if k != 'verified_rows'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
