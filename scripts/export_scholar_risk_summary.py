#!/usr/bin/env python3
"""Export only final risk papers, with prose verified against published PDFs.

Final result.reason is the shared scientific text. Fail rather than silently
rewrite an outdated PDF or introduce an independently authored Word conclusion.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import quote
import zipfile

import fitz
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Cm, Pt, RGBColor

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from modules.chinese_report_generator import doi_to_slug
from modules.review_public_wording import public_text

PROCESS_WORDING=re.compile(r'撤回|降级|升级|恢复高风险|维持高风险|此前判定|原报告|旧报告|上一轮|上一版|现予纠正')


def normalized(text):
    return re.sub(r'\s+','',text)


def verified_paragraphs(result, pdf):
    if result['result']!='高风险':
        raise ValueError('Only final risk papers belong in the summary')
    paragraphs=[public_text(p.strip()) for p in result['reason'].splitlines() if p.strip()]
    if not paragraphs or any(PROCESS_WORDING.search(p) for p in paragraphs):
        raise ValueError('Final PDF/summary content must describe findings, not review history')
    with fitz.open(pdf) as doc:
        text=normalized(''.join(page.get_text() for page in doc))
    if '复核结论：有风险' not in text:
        raise ValueError(f'PDF risk conclusion does not match final result: {pdf}')
    for p in paragraphs:
        if normalized(p) not in text:
            raise ValueError(f'Summary paragraph is not present in final PDF: {pdf}\n{p}')
    return paragraphs


def font(obj,size=10.5,bold=False,color='263445'):
    f=obj.font if hasattr(obj,'font') else obj
    f.name='Microsoft YaHei';f.size=Pt(size);f.bold=bold;f.color.rgb=RGBColor.from_string(color)
    pr=f._element.get_or_add_rPr();fonts=pr.rFonts
    if fonts is None:
        fonts=OxmlElement('w:rFonts');pr.insert(0,fonts)
    for attr in ('ascii','hAnsi','eastAsia','cs'):
        fonts.set(qn('w:'+attr),'Microsoft YaHei')


def link(paragraph,label,url):
    h=OxmlElement('w:hyperlink');h.set(qn('r:id'),paragraph.part.relate_to(url,RT.HYPERLINK,is_external=True))
    r=OxmlElement('w:r');props=OxmlElement('w:rPr')
    for name,val in [('color','176B9B'),('u','single')]:
        item=OxmlElement('w:'+name);item.set(qn('w:val'),val);props.append(item)
    r.append(props);t=OxmlElement('w:t');t.text=label;r.append(t);h.append(r);paragraph._p.append(h)


def export(results, scholar, college, report_dir, output, base_url):
    selected=[r for r in results if r['result']=='高风险']
    # Complete content checks before creating or replacing delivery artifacts.
    prepared=[]
    for r in selected:
        pdf=report_dir/('review_'+doi_to_slug(r['doi'])+'.pdf')
        paragraphs=verified_paragraphs(r,pdf)
        paper=json.loads(Path(r['_report_json']).read_text())['paper']
        prepared.append((r,paper['title'],pdf,paragraphs))
    if output.exists():
        backup=output.with_name(output.name+'_backup_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        shutil.move(str(output),str(backup))
    output.mkdir(parents=True)
    reports=output/'有风险论文复核报告';reports.mkdir()
    doc=Document();sec=doc.sections[0]
    sec.page_width=Cm(21);sec.page_height=Cm(29.7)
    sec.top_margin=sec.bottom_margin=Cm(1.8);sec.left_margin=sec.right_margin=Cm(2.2)
    normal=doc.styles['Normal'];font(normal.font)
    normal.paragraph_format.line_spacing=1.25;normal.paragraph_format.space_after=Pt(7)
    for name,size,color in [('Title',22,'17324D'),('Heading 1',13,'226B74')]:
        style=doc.styles[name];font(style.font,size,True,color)
        style.paragraph_format.keep_with_next=True
        style.paragraph_format.space_before=Pt(12);style.paragraph_format.space_after=Pt(7)
    font(sec.header.paragraphs[0].add_run(f'论文复核情况汇总 | {scholar} · {college}'),8,color='708090')
    footer=sec.footer.paragraphs[0];footer.alignment=WD_ALIGN_PARAGRAPH.RIGHT
    page=OxmlElement('w:fldSimple');page.set(qn('w:instr'),'PAGE');footer._p.append(page)
    doc.add_paragraph(f'{scholar}｜有风险论文分析','Title')
    doc.add_paragraph(college)
    if prepared:
        doc.add_paragraph(f'本总结列出{len(prepared)}篇有风险论文及其核查依据。分析内容与对应复核PDF一致。有风险表示存在需要解释或更正的具体问题，不等于已经证明数据造假。')
    else:
        doc.add_paragraph('经复核，该学者纳入本次核查的论文均未发现需要列出的风险问题。各篇复核PDF已单独发布，可在系统中查看。')
    manifest=[]
    for i,(r,title,pdf,paragraphs) in enumerate(prepared,1):
        doc.add_paragraph(f'{i}. {title}','Heading 1')
        p=doc.add_paragraph(f'DOI：{r["doi"]}　');p.paragraph_format.keep_with_next=True
        url=base_url.rstrip('/')+'/'+quote(pdf.name)
        link(p,'查看复核PDF',url)
        for text in paragraphs:
            p=doc.add_paragraph(text);p.paragraph_format.keep_together=True
        # ASCII file names remain short on Windows even for URL-based paper IDs.
        target=reports/f'{i:02d}_review_report.pdf';shutil.copy2(pdf,target)
        manifest.append({'title':title,'doi':r['doi'],'review_result':r['result'],
                         'pdf':str(target.relative_to(output)),'url':url,
                         'sha256':hashlib.sha256(pdf.read_bytes()).hexdigest(),
                         'analysis':paragraphs})
    doc.core_properties.title=f'{scholar}有风险论文分析';doc.core_properties.author='论文复核'
    word=output/f'{scholar}论文复核情况汇总.docx';doc.save(word)
    text='\n'.join(p.text for p in Document(word).paragraphs)
    assert not PROCESS_WORDING.search(text)
    for r in results:
        if r['result']=='低风险':
            title=json.loads(Path(r['_report_json']).read_text())['paper']['title']
            assert title not in text, title
    for _,_,_,paras in prepared:
        assert all(p in text for p in paras)
    (output/'报告清单.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    zipped=output.with_suffix('.zip')
    with zipfile.ZipFile(zipped,'w',zipfile.ZIP_DEFLATED) as z:
        for file in sorted(output.rglob('*')):
            if file.is_file():z.write(file,Path(scholar+'_论文复核归档')/file.relative_to(output))
    with zipfile.ZipFile(zipped) as z:
        assert z.testzip() is None
        assert len([n for n in z.namelist() if n.endswith('.pdf')])==len(prepared)
        for file in [word,*reports.glob('*.pdf')]:
            assert z.read(str(Path(scholar+'_论文复核归档')/file.relative_to(output)))==file.read_bytes()
    return {'word':str(word),'archive':str(zipped),'risk_papers':len(prepared),'manifest':manifest}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--results',type=Path,required=True)
    p.add_argument('--scholar',required=True);p.add_argument('--college',required=True)
    p.add_argument('--report-dir',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--base-url',required=True)
    a=p.parse_args()
    result=export(json.loads(a.results.read_text()),a.scholar,a.college,a.report_dir,a.output,a.base_url)
    print(json.dumps({k:v for k,v in result.items() if k!='manifest'},ensure_ascii=False))
