#!/usr/bin/env python3
"""Create one concise linked Word summary per scholar for the 1027 batch."""
from __future__ import annotations
import json, re
from pathlib import Path
from urllib.parse import quote
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Cm, Pt, RGBColor

BASE=Path('/home/sjtu/paper-integrity-checker')
ROUND=BASE/'data/yujing_scholar_1027_review_20261027'
RESULTS=ROUND/'final_results'
OUT=ROUND/'final_word'
NAVY='17324D'; TEAL='226B74'

POINTS={
'10.1115/1.4046976':[
'第5—6页式(40)—(48)混用相对O点力矩和Q点虚位移；在单转轴反例中会重复计算外力功，影响力灵敏度公式。',
'第8页Fig.7与第12页Table 2同条件数值不一致：s2约0.60–0.65对0.402，s3约0.90–1.00对0.436，不能由取整解释。'],
'10.1177/09544062251348831':[
'第10页把μPmax定义为柔度/变形指标并要求取小；第12、16页却称取大有利于减小变形，指标选优方向前后冲突。',
'第18页把407×1.5写成614 mm，正确结果为610.5 mm；属于需要更正的算术错误。'],
'10.1002/adrr.202500095':[
'按第11页式(2)、Table 3和Table A1复算，NeRmo约0.688而正文写344，KangaBot约10.136而写73，影响“NeRmo最佳”的排序。',
'AeroTail按第14页式(3)复算约4.0，正文写28；统一单位换算不能同时解释。',
'TAYLRoACH在Table 5写0.4 kg/500 mm，Table A1写0.0105 kg/102 mm，同一型号数据不一致。'],
'10.1016/j.mechmachtheory.2023.105516':[
'第9页式(8)把已经表示为功的δW再次按时间积分，右侧成为功×时间，不能直接等于左侧总功W；第10页和第31页附录再次使用该式。'],
'10.53941/ijamm.2026.100002':[
'Table 1把HASEL响应指标列为25 Hz，但来源论文给出的两种手指带宽为15.9 Hz和8.34 Hz；比较口径不一致。',
'第2页把比功0.38 kJ/kg和比功率1.42 kW/kg合并为一个指标，需分开标注。',
'第5页把4个压电手指、12个自由度写成12个夹层驱动单元，结构数量表述不一致。'],
'10.1111/jipb.13069':['第8页“细胞更少/更多”是可由上下文纠正的笔误，建议改为“细胞更小、数量更多”；其余区室和调控者表述差异可由上下文解释。'],
'10.12146/j.issn.2095-3135.20220510001':['第9页正文将图10、图11对应方案写成方案3，实际应为方案1；表2数值可复算，属于方案编号勘误。'],
'10.1016/j.jprot.2023.104903':['Supplemental Table 2组标签、摘要统计重复表述及候选计数/样本归属口径需统一或说明；现有材料不足以证明原始测量值逐值复用。']}

def url(doi):
    return 'http://202.120.57.100/review_reports/yujing_scholar/review_'+doi.replace('/','_')+'.pdf'

def font(run,size=10.5,bold=False,color='263445'):
    target = run.font if hasattr(run, 'font') else run
    target.name='Microsoft YaHei'; target.size=Pt(size); target.bold=bold; target.color.rgb=RGBColor.from_string(color)
    rpr=target._element.get_or_add_rPr(); rfonts=rpr.rFonts
    if rfonts is None: rfonts=OxmlElement('w:rFonts'); rpr.insert(0,rfonts)
    for key in ('ascii','hAnsi','eastAsia','cs'): rfonts.set(qn('w:'+key),'Microsoft YaHei')

def link(p,text,target):
    h=OxmlElement('w:hyperlink'); h.set(qn('r:id'),p.part.relate_to(target,RT.HYPERLINK,is_external=True))
    r=OxmlElement('w:r'); pr=OxmlElement('w:rPr')
    for tag,val in [('color','176B9B'),('u','single')]:
        e=OxmlElement('w:'+tag); e.set(qn('w:val'),val); pr.append(e)
    r.append(pr); t=OxmlElement('w:t'); t.text=text; r.append(t); h.append(r); p._p.append(h)

def title_for(scholar,doi):
    slug=doi.replace('/','_')
    p=ROUND/'papers'/scholar/slug/'initial_report.json'
    try: return json.load(p.open())['paper']['title']
    except Exception: return doi

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    for scholar in ['戴大伟','韩有承']:
        rows=json.load((RESULTS/f'{scholar}.json').open())
        high=[r for r in rows if r['result']=='高风险']; low=[r for r in rows if r['result']=='低风险']
        d=Document(); sec=d.sections[0]; sec.page_width=Cm(21); sec.page_height=Cm(29.7)
        sec.top_margin= Cm(2); sec.bottom_margin=Cm(1.9); sec.left_margin=Cm(2.2); sec.right_margin=Cm(2.2)
        normal=d.styles['Normal']; font(normal.font,10.5); normal.paragraph_format.line_spacing=1.25; normal.paragraph_format.space_after=Pt(7)
        for name,size,color in [('Title',23,NAVY),('Heading 1',17,NAVY),('Heading 2',12,TEAL),('Heading 3',11,NAVY)]:
            st=d.styles[name]; font(st.font,size,True,color); st.paragraph_format.space_before=Pt(12); st.paragraph_format.space_after=Pt(7); st.paragraph_format.keep_with_next=True
        d.add_paragraph(scholar+'｜论文复核情况汇总','Title')
        p=d.add_paragraph('1027批次当前模型复核结论'); font(p.runs[0].font,10,color='708090')
        d.add_paragraph(f'本次整理{scholar}的共计{len(rows)}篇论文，其中{len(high)}篇列为有风险论文，{len(low)}篇未发现风险。有风险表示存在需要作者解释、补充材料或更正的具体异常，不等同于已经证明存在数据造假。')
        t=d.add_table(rows=1,cols=3); t.autofit=False
        for c,label,val in zip(t.rows[0].cells,['论文总数','有风险论文','未发现风险'],[len(rows),len(high),len(low)]):
            c.width=Cm(5.5); sh=OxmlElement('w:shd'); sh.set(qn('w:fill'),'EDF3F7'); c._tc.get_or_add_tcPr().append(sh)
            p=c.paragraphs[0]; p.alignment=WD_ALIGN_PARAGRAPH.CENTER; font(p.add_run(f'{val}篇\n'),18,True,NAVY); font(p.add_run(label),10,False,'52657A')
        d.add_paragraph('以下问题按重要程度列出，措辞与对应复核PDF保持一致。')
        d.add_paragraph(f'有风险论文（{len(high)}篇）','Heading 1')
        if not high: d.add_paragraph('本批次没有列为有风险的论文。')
        for i,r in enumerate(high,1):
            doi=r['doi']; d.add_paragraph(f'{i}. {title_for(scholar,doi)}','Heading 2')
            p=d.add_paragraph(); font(p.add_run('DOI：'+doi+'    '),9,False,'52657A'); link(p,'查看复核PDF',url(doi))
            for j,x in enumerate(POINTS.get(doi,[r.get('reason','')]),1): d.add_paragraph(f'（{j}）{x}')
        d.add_paragraph(f'未发现风险论文（{len(low)}篇）','Heading 1')
        if not low: d.add_paragraph('本批次没有未发现风险的论文。')
        for i,r in enumerate(low,1):
            doi=r['doi']; d.add_paragraph(f'{i}. {title_for(scholar,doi)}','Heading 2')
            p=d.add_paragraph(); font(p.add_run('DOI：'+doi+'    '),9,False,'52657A'); link(p,'查看复核PDF',url(doi))
            for j,x in enumerate(POINTS.get(doi,[r.get('reason','未发现风险')]),1): d.add_paragraph(f'（{j}）{x}')
        d.core_properties.title=scholar+'论文复核情况汇总'; d.core_properties.author='论文复核'
        out=OUT/(scholar+'论文复核情况汇总.docx'); d.save(out); print(out)

if __name__=='__main__': main()
