#!/usr/bin/env python3
"""Archive the 30-paper Han Youcheng round and create its linked Word summary."""
from __future__ import annotations
import json, shutil
from pathlib import Path
from urllib.parse import unquote
import pymssql
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Cm, Pt, RGBColor

BASE=Path('/home/sjtu/paper-integrity-checker')
ROUND=BASE/'data/yujing_scholar_1027_review_20261027'
PAPERS=ROUND/'papers'/'韩有承'
PUBLIC=BASE/'data/output/yujing_scholar/review_v2'
ARCHIVE=ROUND/'韩有承_30篇复核PDF'
WORD=ROUND/'韩有承30篇论文复核情况汇总.docx'
NAVY='17324D'; TEAL='226B74'

POINTS={
'10.1115/1.4046976':[
'第5—6页式(40)—(48)混用相对O点力矩和Q点虚位移；在单转轴反例中会重复计算外力功，影响力灵敏度公式。',
'第8页Fig.7与第12页Table 2同条件数值不一致：s2约0.60–0.65对0.402，s3约0.90–1.00对0.436，不能由取整解释。'],
'10.1177/09544062251348831':[
'第10页将μPmax定义为柔度/变形指标并要求取小；第12、16页却称取大有利于减小变形，指标选优方向前后冲突。',
'第18页将407×1.5写成614 mm，正确结果为610.5 mm，属于需要更正的算术错误。'],
'10.1002/adrr.202500095':[
'按第11页式(2)、Table 3和Table A1复算，NeRmo约0.688而正文写344，KangaBot约10.136而写73，影响“NeRmo最佳”的排序。',
'AeroTail按第14页式(3)复算约4.0，正文写28；统一单位换算不能同时解释。',
'TAYLRoACH在Table 5写0.4 kg/500 mm，Table A1写0.0105 kg/102 mm，同一型号数据不一致。'],
'10.1016/j.mechmachtheory.2023.105516':[
'第9页式(8)把已经表示为功的δW再次按时间积分，右侧成为功×时间，不能直接等于左侧总功W；第10页和第31页附录再次使用该式。'],
'10.53941/ijamm.2026.100002':[
'Table 1把HASEL响应指标列为25 Hz，但来源论文给出的两种手指带宽为15.9 Hz和8.34 Hz，比较口径不一致。',
'第2页把比功0.38 kJ/kg和比功率1.42 kW/kg合并为一个指标，需分开标注。',
'第5页把4个压电手指、12个自由度写成12个夹层驱动单元，结构数量表述不一致。']}

def fnt(obj,size=10.5,bold=False,color='263445'):
    t=obj.font if hasattr(obj,'font') else obj
    t.name='Microsoft YaHei'; t.size=Pt(size); t.bold=bold; t.color.rgb=RGBColor.from_string(color)
    rpr=t._element.get_or_add_rPr(); rfonts=rpr.rFonts
    if rfonts is None: rfonts=OxmlElement('w:rFonts'); rpr.insert(0,rfonts)
    for key in ('ascii','hAnsi','eastAsia','cs'): rfonts.set(qn('w:'+key),'Microsoft YaHei')

def hyperlink(p,text,target):
    h=OxmlElement('w:hyperlink'); h.set(qn('r:id'),p.part.relate_to(target,RT.HYPERLINK,is_external=True))
    r=OxmlElement('w:r'); pr=OxmlElement('w:rPr')
    for tag,val in [('color','176B9B'),('u','single')]:
        e=OxmlElement('w:'+tag); e.set(qn('w:val'),val); pr.append(e)
    r.append(pr); tx=OxmlElement('w:t'); tx.text=text; r.append(tx); h.append(r); p._p.append(h)

def db_rows():
    cn=pymssql.connect(server='10.119.5.44',user='yujing',password='fengxian_YJ514',database='lunwenyujing',charset='utf8',login_timeout=10,timeout=60)
    cur=cn.cursor(as_dict=True)
    cur.execute("SELECT doi, review_result, review_report_url, status FROM yujing_scholar WHERE scholar=%s ORDER BY id",('韩有承',))
    rows=cur.fetchall(); cn.close()
    return rows

def title(doi):
    p=PAPERS/doi.replace('/','_')/'initial_report.json'
    try: return json.load(p.open(encoding='utf-8'))['paper']['title']
    except Exception: return doi

def main():
    rows=db_rows()
    if len(rows)!=30: raise RuntimeError(f'expected 30 Han records, found {len(rows)}')
    if any(r['status']!=2 or r['review_result'] not in ('高风险','低风险') for r in rows):
        raise RuntimeError('not all 30 records have a final review result/status=2')
    ARCHIVE.mkdir(parents=True,exist_ok=True)
    copied=[]
    for r in rows:
        url=r['review_report_url']; name=unquote(url.rsplit('/',1)[-1]); src=PUBLIC/name
        if not src.is_file(): raise FileNotFoundError(src)
        dst=ARCHIVE/name; shutil.copy2(src,dst); copied.append((r['doi'],name,r['review_result'],url))
    (ARCHIVE/'README.txt').write_text('韩有承30篇论文复核PDF归档\n有风险：5篇；未见风险：25篇。\n文件均来自当前公开复核链接对应的最新PDF。\n'+'\n'.join(f'{doi}\t{result}\t{url}' for doi,name,result,url in copied),encoding='utf-8')
    high=[r for r in rows if r['review_result']=='高风险']; low=[r for r in rows if r['review_result']=='低风险']
    doc=Document(); sec=doc.sections[0]; sec.page_width=Cm(21); sec.page_height=Cm(29.7); sec.top_margin=Cm(2); sec.bottom_margin=Cm(1.9); sec.left_margin=Cm(2.2); sec.right_margin=Cm(2.2)
    normal=doc.styles['Normal']; fnt(normal.font); normal.paragraph_format.line_spacing=1.25; normal.paragraph_format.space_after=Pt(7)
    for n,size,col in [('Title',23,NAVY),('Heading 1',17,NAVY),('Heading 2',12,TEAL),('Heading 3',11,NAVY)]:
        st=doc.styles[n]; fnt(st.font,size,True,col); st.paragraph_format.space_before=Pt(12); st.paragraph_format.space_after=Pt(7); st.paragraph_format.keep_with_next=True
    doc.add_paragraph('韩有承｜30篇论文复核情况汇总','Title')
    p=doc.add_paragraph('1027批次全量复核结论'); fnt(p.runs[0].font,10,False,'708090')
    doc.add_paragraph('本次整理韩有承共计30篇论文，其中5篇列为有风险论文，25篇未见风险。有风险表示存在需要作者解释或补充材料的具体异常，不等同于已经证明存在数据造假；未见风险表示在本次取得的材料和核验范围内未发现足以判定为有风险的明确证据。')
    table=doc.add_table(rows=1,cols=3); table.autofit=False
    for c,label,val in zip(table.rows[0].cells,['论文总数','有风险论文','未见风险'],[30,5,25]):
        c.width=Cm(5.5); sh=OxmlElement('w:shd'); sh.set(qn('w:fill'),'EDF3F7'); c._tc.get_or_add_tcPr().append(sh); p=c.paragraphs[0]; p.alignment=WD_ALIGN_PARAGRAPH.CENTER; fnt(p.add_run(f'{val}篇\n'),18,True,NAVY); fnt(p.add_run(label),10,False,'52657A')
    doc.add_paragraph('以下问题按重要程度列出，措辞与对应复核PDF保持一致。')
    doc.add_paragraph('有风险论文（5篇）','Heading 1')
    for i,r in enumerate(high,1):
        doi=r['doi']; doc.add_paragraph(f'{i}. {title(doi)}','Heading 2'); p=doc.add_paragraph(); fnt(p.add_run('DOI：'+doi+'    '),9,False,'52657A'); hyperlink(p,'查看复核PDF',r['review_report_url'])
        for j,x in enumerate(POINTS.get(doi,['报告中列出的具体异常需作者核对。']),1): doc.add_paragraph(f'（{j}）{x}')
    doc.add_paragraph('未见风险论文（25篇）','Heading 1')
    doc.add_paragraph('其余25篇论文在本次取得的材料和核验范围内未见风险。')
    for i,r in enumerate(low,1):
        doi=r['doi']; doc.add_paragraph(f'{i}. {title(doi)}','Heading 3'); p=doc.add_paragraph(); fnt(p.add_run('DOI：'+doi+'    '),9,False,'52657A'); hyperlink(p,'查看复核PDF',r['review_report_url'])
    doc.core_properties.title='韩有承30篇论文复核情况汇总'; doc.core_properties.author='论文复核'; doc.save(WORD)
    # Re-open and validate link count and generated archive.
    d=Document(WORD); links=[x.target_ref for x in d.part.rels.values() if x.reltype==RT.HYPERLINK]
    if len(links)!=30 or not all(x.startswith('http://') for x in links): raise RuntimeError('Word hyperlink validation failed')
    if len(list(ARCHIVE.glob('review_*.pdf')))!=30: raise RuntimeError('archive PDF count validation failed')
    print(json.dumps({'word':str(WORD),'archive':str(ARCHIVE),'pdf_count':30,'high':5,'low':25},ensure_ascii=False))

if __name__=='__main__': main()
