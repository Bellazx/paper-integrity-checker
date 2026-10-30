"""Create a styled, source-aligned Word summary with verified public PDF links."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
import shutil
import urllib.request
import zipfile
from collections import Counter

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from docx.opc.constants import RELATIONSHIP_TYPE as RT

import rewrite_astra_highrisk_reports as reports
from modules.review_public_wording import public_text

DOC_NAME = '三位学者48篇论文复核情况汇总.docx'
SCHOLARS = ['胡高伟', '樊令仲', '王丹丹']
NAVY = '17324D'
TEAL = '226B74'


def set_font(font, size, *, bold=False, color='263445'):
    font.name = 'Microsoft YaHei'
    font.size = Pt(size)
    font.bold = bold
    font.color.rgb = RGBColor.from_string(color)
    rpr = font._element.get_or_add_rPr()
    rfonts = rpr.rFonts
    if rfonts is None:
        rfonts = OxmlElement('w:rFonts')
        rpr.insert(0, rfonts)
    for key in ('ascii', 'hAnsi', 'eastAsia', 'cs'):
        rfonts.set(qn('w:' + key), 'Microsoft YaHei')


def hyperlink(paragraph, text, url):
    link = OxmlElement('w:hyperlink')
    link.set(qn('r:id'), paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True))
    run = OxmlElement('w:r')
    props = OxmlElement('w:rPr')
    for tag, value in [('color', '176B9B'), ('u', 'single'), ('sz', '20')]:
        item = OxmlElement('w:' + tag)
        item.set(qn('w:val'), value)
        props.append(item)
    run.append(props)
    node = OxmlElement('w:t')
    node.text = text
    run.append(node)
    link.append(run)
    paragraph._p.append(link)


def verify_pdf(doi):
    url = reports.public_pdf_url(doi)
    with urllib.request.urlopen(url, timeout=30) as response:
        content = response.read()
        assert response.status == 200 and content.startswith(b'%PDF-'), url
    name = 'review_' + doi.replace('/', '_').replace('－', '') + '.pdf'
    assert content == (reports.OUT / name).read_bytes(), f'Public PDF is outdated: {doi}'
    return {'doi': doi, 'url': url, 'sha256': hashlib.sha256(content).hexdigest(),
            'matches_current_pdf': True}


def word_summary(item):
    return public_text(reports.public_summary(item))


def create_document(path, scholar):
    papers = {doi: item for doi, item in reports.PAPERS.items() if item['scholar'] == scholar}
    totals = Counter(row['scholar'] for row in json.loads((reports.ROUND / 'papers.json').read_text()))
    no_risk_count = totals[scholar] - len(papers)
    intro = (f'本次整理{scholar}的共计{totals[scholar]}篇论文，其中{len(papers)}篇列为有风险论文，'
             f'其余{no_risk_count}篇在本次核验范围内未发现风险。'
             '有风险论文包括需作者更正的图文、数字错误，以及需作者解释或补充材料的具体异常，'
             '不等同于已经证明存在数据造假。')
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin, section.bottom_margin = Cm(2), Cm(1.9)
    section.left_margin, section.right_margin = Cm(2.2), Cm(2.2)
    section.header_distance = section.footer_distance = Cm(0.9)
    normal = doc.styles['Normal']
    set_font(normal.font, 10.5)
    normal.element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
    normal.paragraph_format.line_spacing = 1.25
    normal.paragraph_format.space_after = Pt(7)
    normal.paragraph_format.widow_control = True
    for name, size, color in [('Title', 23, NAVY), ('Heading 1', 17, NAVY),
                              ('Heading 2', 12, TEAL), ('Heading 3', 11, NAVY)]:
        style = doc.styles[name]
        set_font(style.font, size, bold=True, color=color)
        style.element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), 'Noto Sans CJK SC')
        style.paragraph_format.space_before = Pt(12)
        style.paragraph_format.space_after = Pt(7)
        style.paragraph_format.keep_with_next = True
    header = section.header.paragraphs[0]
    set_font(header.add_run('论文复核情况汇总  |  ' + scholar).font, 8, color='708090')
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    set_font(footer.add_run('论文复核汇总  ·  ').font, 8, color='708090')
    field = OxmlElement('w:fldSimple')
    field.set(qn('w:instr'), 'PAGE')
    footer._p.append(field)
    doc.add_paragraph(scholar + '｜论文复核情况汇总', 'Title')
    p = doc.add_paragraph('有风险论文与未发现风险论文汇总    ·    2026年10月23日')
    set_font(p.runs[0].font, 10, color='708090')
    doc.add_paragraph(intro)
    table = doc.add_table(rows=1, cols=3)
    table.autofit = False
    for cell, label, value in zip(table.rows[0].cells,
                                  ['论文总数', '有风险论文', '未发现风险'],
                                  [f'{totals[scholar]}篇', f'{len(papers)}篇', f'{no_risk_count}篇']):
        cell.width = Cm(5.5)
        shade = OxmlElement('w:shd')
        shade.set(qn('w:fill'), 'EDF3F7')
        cell._tc.get_or_add_tcPr().append(shade)
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(8)
        set_font(p.add_run(value + '\n').font, 18, bold=True, color=NAVY)
        set_font(p.add_run(label).font, 10, color='52657A')
    p = doc.add_paragraph(f'以下列出{len(papers)}篇有风险论文；问题按重要程度排列，具体问题及核验依据与复核PDF一致。')
    p.paragraph_format.space_before = Pt(8)
    set_font(p.runs[0].font, 9, color='708090')
    for index, scholar in enumerate([scholar]):
        p = doc.add_paragraph(scholar, 'Heading 1')
        if index:
            p.paragraph_format.page_break_before = True
        for risk, category in [(True, '有风险论文')]:
            selected = [(doi, item) for doi, item in reports.PAPERS.items()
                        if item['scholar'] == scholar and item['risk'] == risk]
            doc.add_paragraph(f'{category}（{len(selected)}篇）', 'Heading 2')
            if not selected:
                doc.add_paragraph('本次整理范围内无此类论文。')
            for number, (doi, item) in enumerate(selected, 1):
                doc.add_paragraph(f'{number}. {item["title"]}', 'Heading 3')
                p = doc.add_paragraph()
                p.paragraph_format.keep_with_next = True
                set_font(p.add_run('DOI：' + doi + '    |    ').font, 9, color='52657A')
                hyperlink(p, '查看复核PDF', reports.public_pdf_url(doi))
                for point in word_summary(item).splitlines():
                    p = doc.add_paragraph()
                    p.paragraph_format.keep_together = True
                    lead, separator, rest = point.partition('。')
                    p.add_run(lead + separator).bold = True
                    p.add_run(rest)
    doc.add_paragraph(f'未发现风险（{no_risk_count}篇）', 'Heading 2')
    doc.add_paragraph(f'其余{no_risk_count}篇论文在本次取得的材料及核验范围内未发现风险。')
    doc.core_properties.title = scholar + '论文复核情况汇总'
    doc.core_properties.subject = '有风险论文与未发现风险论文汇总'
    doc.core_properties.author = '论文复核'
    doc.save(path)
    reopened = Document(path)
    text = '\n'.join(p.text for p in reopened.paragraphs)
    assert intro in text
    assert '需要注意' not in text and '配色' not in text and '颜色含义' not in text
    for item in papers.values():
        for paragraph in word_summary(item).splitlines():
            assert paragraph in text
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.endswith('.xml'):
                assert '高风险' not in archive.read(name).decode('utf-8'), name
    links = [rel.target_ref for rel in reopened.part.rels.values() if rel.reltype == RT.HYPERLINK]
    assert len(links) == len(papers)
    assert set(links) == {reports.public_pdf_url(doi) for doi in papers}
    assert all(url.startswith('http://') for url in links)


def main():
    with ThreadPoolExecutor(max_workers=5) as pool:
        checks = list(pool.map(verify_pdf, reports.PAPERS))
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d_%H%M%S')
    backup = reports.ROUND / ('summary_word_backup_' + stamp)
    backup.mkdir()
    for folder, summary in [(reports.GROUPED, '论文复核汇总.md'),
                            (reports.ARCHIVE, 'summary_astra_20261022.md')]:
        shutil.copy2(folder / summary, backup / summary)
        if (folder / DOC_NAME).exists():
            shutil.copy2(folder / DOC_NAME, backup / (folder.name + '.docx'))
    for folder in (reports.GROUPED, reports.ARCHIVE):
        if (folder / DOC_NAME).exists():
            shutil.move(str(folder / DOC_NAME), str(backup / (folder.name + '_superseded.docx')))
    words = []
    for scholar in SCHOLARS:
        word = reports.GROUPED / scholar / (scholar + '论文复核情况汇总.docx')
        if word.exists():
            shutil.copy2(word, backup / word.name)
        create_document(word, scholar)
        shutil.copy2(word, reports.ARCHIVE / word.name)
        words.append(word)
    reports.write_summary(reports.GROUPED / '论文复核汇总.md', grouped=True)
    reports.write_summary(reports.ARCHIVE / 'summary_astra_20261022.md', grouped=False)
    (reports.GROUPED / 'HTTP链接校验.json').write_text(json.dumps(
        {'checked_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), 'pdfs': checks},
        ensure_ascii=False, indent=2))
    for folder in (reports.GROUPED, reports.ARCHIVE):
        shutil.make_archive(str(folder), 'zip', root_dir=folder)
        with zipfile.ZipFile(folder.with_suffix('.zip')) as zipped:
            assert zipped.testzip() is None
            for word in words:
                relative = word.relative_to(reports.GROUPED) if folder == reports.GROUPED else word.name
                assert zipped.read(str(relative)) == word.read_bytes()
    print(json.dumps({'words': [str(word) for word in words], 'verified_http_links': len(checks),
                      'summary_paragraphs_match_pdfs': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
