#!/usr/bin/env python3
"""Build the final, manually adjudicated review inputs for the 1027 batch.

The eight records here are deliberately explicit: they are the conclusions from
the current-model recheck, not a mechanical copy of the first-pass detector.
"""
from __future__ import annotations
import copy, json, glob
from pathlib import Path

ROOT = Path('/home/sjtu/paper-integrity-checker/data/yujing_scholar_1027_review_20261027/papers')
OUT = Path('/home/sjtu/paper-integrity-checker/data/yujing_scholar_1027_review_20261027/final_results')
EVID = Path('/home/sjtu/paper-integrity-checker/data/yujing_scholar_1027_review_20261027/final_evidence')

def load_current(scholar, slug):
    p = ROOT / scholar / slug / 'results_current_model.json'
    if p.exists(): return json.load(p.open())[0]
    p = ROOT / scholar / slug / 'results.json'
    return json.load(p.open())[0]

def common_low(d, reason, data, image=None):
    d = copy.deepcopy(d)
    d.update(result='低风险', verdict='建议低风险', trigger='发现可定位的文字、引注、格式或方案对应问题，已记录更正建议；未确认会改变数据、图表、计算或核心结论的独立实质问题。', reason=reason, data_review=data)
    if image is not None: d['image_review'] = image
    d['dimension_verdicts'] = {'image':'建议低风险','data':'建议低风险','ref':'建议低风险','methodology':'建议低风险'}
    return d

def common_high(d, trigger, reason, data, method, evidence):
    d = copy.deepcopy(d)
    d.update(result='高风险', verdict='建议高风险', trigger=trigger, reason=reason,
             data_review=data, methodology_review=method, evidence_images=evidence,
             dimension_verdicts={'image':'建议低风险','data':'建议高风险','ref':'建议低风险','methodology':'建议高风险'})
    return d

def ev(path, dim, idx, cap):
    return {'dimension':dim,'issue_index':idx,'path':str(path),'caption':cap}

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    EVID.mkdir(parents=True, exist_ok=True)
    final = {'戴大伟': [], '韩有承': []}
    # Low-risk records
    jipb = load_current('戴大伟','10.1111_jipb.13069')
    jipb['image_review'] = '图1（第2页）和图2（第3页）已渲染查看，并与图注、正文逐项核对；未发现真实图像重复、拼接或图件错配。图1图注列出五个区室，而摘要和正文首段写四个，属于需要统一的文字表述。'
    jipb['data_review'] = '未提供可解析的源数据表。已核查：第8页RBR1段把“细胞更少”和“细胞更多”写在同一句，结合所引研究应核对为“细胞更小、数量更多”，属于明确文字勘误；四/五区室及ESR调控者的差异结合上下文可以解释，未确认数据、样本量或计算结果异常。'
    jipb['ref_review'] = '参考文献条目与正文引用可追溯，未发现会改变论断对象的实质性引用问题。'
    jipb['methodology_review'] = '本文为综述，无作者自有实验统计；未发现独立的方法学风险。'
    jipb['reason'] = '图1、图2与正文和图注核对后未发现图像完整性问题；第8页“细胞更少/更多”是可由上下文纠正的笔误，四/五区室及ESR调控者范围差异可以解释。未发现足以判定为有风险的数据、图表或计算异常，故未发现风险。'
    jipb['dimension_verdicts']={'image':'建议低风险','data':'建议低风险','ref':'建议低风险','methodology':'建议低风险'}
    jipb['evidence_images']=[]
    final['戴大伟'].append(jipb)
    jprot = load_current('戴大伟','10.1016_j.jprot.2023.104903')
    jprot = common_low(jprot,
        '图像与源数据核对未确认实质性异常。需要作者更正或说明的事项为：Supplemental Table 2组标签、摘要统计的重复表述，以及候选计数和样本归属口径。现有材料不能证明原始测量值被逐值复用，故未发现风险。',
        'Supplemental Table 2的组名和表格说明需统一；摘要中的统计数字有重复表述；候选计数应区分诱饵排除、登录号版本及检出/筛选口径。3次生物学实验×60对气孔不能直接当作180个独立生物学重复，现有材料不足以据此认定原始数据复用。',
        'Fig.1–Fig.6及补充图已渲染查看。第5页疑似竖直接缝位于面板间版式空白栏，属于排版假阳性；跨图核对未发现不同实验照片被重复使用。')
    jprot['ref_review'] = '参考文献与正文引用可追溯；未发现会改变被引对象的实质性引用问题。'
    jprot['methodology_review'] = '研究设计与统计方面保留样本归属和候选计数的说明事项；现有材料不足以确认原始测量值复用，也未确认统计结论必然失效。'
    # Low-risk report keeps the findings in prose; screenshots are reserved for
    # the five retained risk reports so each image is bound to a numbered issue.
    jprot['evidence_images']=[]
    jprot['excel_evidence_regions']=[]
    final['戴大伟'].append(jprot)
    cn = load_current('韩有承','10.12146_j.issn.2095-3135.20220510001')
    cn['image_review'] = '图10、图11及相关图件已渲染查看，并与第9页正文、方案定义和图注逐项核对；未发现图像重复、拼接或图件错配。图10/图11实际对应方案1，而第9页文字写成方案3，属于可直接更正的方案编号错误。'
    cn['reason'] = '图10、图11实际对应方案1，而第9页正文写成方案3，属于方案编号与图示对应错误。表2中的1.6180和1.0000可按文中定义复算，未确认算法、数据或核心结论失效；未发现足以判定为有风险的实质异常，故未发现风险。'
    cn['ref_review'] = '参考文献维度未发现实质性不一致。'
    cn['methodology_review'] = '方案编号与图示对应错误可直接更正；表2数值可复算，未确认算法或核心结论失效。'
    cn['dimension_verdicts']={'image':'建议低风险','data':'建议低风险','ref':'建议低风险','methodology':'建议低风险'}
    cn['evidence_images']=[]
    final['韩有承'].append(cn)
    # High-risk records
    asme = load_current('韩有承','10.1115_1.4046976')
    asme['trigger']='第5—6页公式(40)—(48)混用参考点，且第8页Fig.7与第12页Table 2同条件数值不一致。'
    asme['data_review']='问题位置：第8页Fig.7与第12页Table 2。问题类型：同条件图表数值不一致。发现：（1）同一姿态、同一载荷方向下，s2图中约0.60–0.65而表中为0.402；s3图中约0.90–1.00而表中为0.436，差异不能由取整解释。为什么重要：读者无法确认力灵敏度指标的定量标度。核查依据：逐项读取Fig.7等值线和Table 2表值。'
    asme['methodology_review']='问题位置：第5—6页式(40)—(48)。问题类型：力矩参考点与虚位移参考点混用。发现：先把外力换算为相对O点的力矩，又与Q点虚位移配对；在单转轴反例中会把同一外力功计算两次。为什么重要：该问题影响纯力到驱动力的映射。核查依据：对照式(37)—(48)的点位定义并代入单转轴反例。'
    asme['image_review']='图7、Table 2和式(48)均已逐项核对；未发现图像重复、拼接或图件错配。'
    asme['ref_review']='参考文献维度未发现会改变论断对象的实质性问题。'
    asme['reason']='有风险依据只有两项：（1）Fig.7与Table 2同条件数值不一致；（2）式(40)—(48)混用力矩与位移参考点，存在重复计算外力功的风险。'
    asme['dimension_verdicts']={'image':'建议低风险','data':'建议高风险','ref':'建议低风险','methodology':'建议高风险'}
    asme['evidence_images']=[ev(ROOT/'韩有承/10.1115_1.4046976/artifacts/rv2_fig7b.png','data',1,'问题1核心证据：第8页Fig.7(b)中s2约0.62。'),ev(ROOT/'韩有承/10.1115_1.4046976/artifacts/rv2_table2.png','data',1,'问题1核心证据：第12页Table 2中s2为0.402、s3为0.436。'),ev(ROOT/'韩有承/10.1115_1.4046976/artifacts/rv2_eq48.png','methodology',1,'问题2核心证据：第6页式(48)的力分支与力矩分支点位口径。')]
    final['韩有承'].append(asme)
    sage = load_current('韩有承','10.1177_09544062251348831')
    sage['trigger']='第10页指标定义与第12、16页取优方向相反；第18页407×1.5算术错误。'
    sage['data_review']='问题位置：第18页阻尼器长度计算。问题类型：算术错误。发现：论文写407×1.5=614 mm，实际为610.5 mm。为什么重要：最终行程数值需要作者明确并更正。核查依据：直接复算第18页算式。'
    sage['methodology_review']='问题位置：第10、12、16页μPmax定义和取优说明。问题类型：指标定义与优化方向冲突。发现：第10页将μPmax定义为柔度/变形指标并要求取小，第12、16页却称取大有利于减小变形。为什么重要：读者无法判断性能图谱实际追求的方向。核查依据：逐段对照式(15)及第12、16页文字。'
    sage['image_review']='23幅主图已渲染并交叉核对；未发现图像重复、拼接或曲线复用。'
    sage['ref_review']='参考文献维度未发现实质性不一致。'
    sage['reason']='有风险依据只有两项：（1）μPmax的定义与取优方向前后冲突；（2）第18页407×1.5写成614 mm，正确为610.5 mm。其余近似和排版问题不作为独立风险依据。'
    sage['dimension_verdicts']={'image':'建议低风险','data':'建议高风险','ref':'建议低风险','methodology':'建议高风险'}
    sage['evidence_images']=[ev(ROOT/'韩有承/10.1177_09544062251348831/artifacts/evidence/rv3_p18_dl4.png','data',1,'问题1核心证据：第18页407×1.5=614 mm的原文算式。'),ev(ROOT/'韩有承/10.1177_09544062251348831/artifacts/evidence/rv3_p10_gsi_eq15.png','methodology',1,'问题2核心证据：第10页μPmax定义及“最小化”说明。')]
    final['韩有承'].append(sage)
    adrr = load_current('韩有承','10.1002_adrr.202500095')
    adrr = common_high(adrr,
        '第9、11、14、16、24页的公式与表格存在三组可复算的定量冲突，影响综述中的性能比较。',
        '本篇有风险结论仅针对三组公式、表格和比较口径冲突；引用编号、型号笔误及角度近似不单独升级。',
        '（1）问题位置：第9页Table 3、第11页式(2)。问题类型：指标数值不一致。发现：NeRmo按表值复算约0.688，正文写344；KangaBot复算约10.136，正文写73，直接影响“NeRmo最佳”的排序。为什么重要：会改变综述中的性能比较。核查依据：代入式(2)并对照Table 3。\n（2）问题位置：第14页式(3)。问题类型：指标数值不一致。发现：AeroTail按表值复算约4.0，正文写28。为什么重要：正文指标无法由表值复算。核查依据：代入式(3)并对照第14页表格。\n（3）问题位置：第16页Table 5与第24页Table A1。问题类型：同一型号数据不一致。发现：TAYLRoACH分别写成0.4 kg/500 mm和0.0105 kg/102 mm。为什么重要：读者无法确认该型号的真实质量和尺寸。核查依据：逐项对照两张表；三组差异不能由统一取整或单位换算同时解释。',
        '研究设计与统计维度未发现独立风险；问题集中在综述中的数值转述和比较口径。',
        [ev(Path('/home/sjtu/paper-integrity-checker/data/yujing_scholar_1027_review_20261027/current_model_recheck_8/adrr_page11.png'),'data',1,'问题1核心证据：第11页式(2)及对应表格。'),ev(Path('/home/sjtu/paper-integrity-checker/data/yujing_scholar_1027_review_20261027/current_model_recheck_8/adrr_page14.png'),'data',2,'问题2核心证据：第14页AeroTail指标。'),ev(Path('/home/sjtu/paper-integrity-checker/data/yujing_scholar_1027_review_20261027/current_model_recheck_8/adrr_page24.png'),'data',3,'问题3核心证据：第24页Table A1中的TAYLRoACH条目。')])
    adrr['dimension_verdicts']={'image':'建议低风险','data':'建议高风险','ref':'建议低风险','methodology':'建议低风险'}
    adrr['image_review']='图像维度：已核对正文图件与PDF内嵌图，未发现图像重复、拼接或错配。'
    adrr['ref_review']='参考文献维度：引用对象和著录未发现会改变本篇风险结论的实质问题。'
    final['韩有承'].append(adrr)
    mech = load_current('韩有承','10.1016_j.mechmachtheory.2023.105516')
    mech = common_high(mech,
        '第9页式(8)的积分对象和量纲前后不一致，且第10页及第31页附录继续使用该式。',
        '本篇有风险结论针对式(8)的积分对象和量纲问题；第15页定理3编号错误是普通勘误，不作为独立风险依据。',
        '未提供可解析的源数据表；数据维度未发现独立风险。图件核对未发现重复或拼接，现有材料不能据此断言后文仿真必然失效。',
        '问题位置：第9页式(8)，第10页正文及第31页附录。问题类型：核心公式积分对象和量纲不一致。发现：（1）式(8)左侧是总功W，右侧却把已经写成δW的功再次按时间积分，单位变成功×时间，不能直接等于W。（2）第10页正文和第31页附录再次引用该式。为什么重要：该式属于理论推导链，量纲不闭合会影响读者判断推导是否成立。核查依据：逐项对照式(8)两侧符号、单位及后文引用。',
        [ev(ROOT/'韩有承/10.1016_j.mechmachtheory.2023.105516/artifacts/myev1_eq7_eq8_page9.png','methodology',1,'第9页式(8)：δW再次按时间积分，需澄清积分对象和量纲。')])
    mech['dimension_verdicts']={'image':'建议低风险','data':'建议低风险','ref':'建议低风险','methodology':'建议高风险'}
    mech['image_review']='图像维度：已核对主图，未发现图像重复、拼接或错配。'
    mech['ref_review']='参考文献维度：未发现会改变本篇风险结论的实质问题。'
    final['韩有承'].append(mech)
    ij = load_current('韩有承','10.53941_ijamm.2026.100002')
    ij = common_high(ij,
        '第2、5、6页及Table 1存在三处可定位的性能和结构口径错误，影响读者对不同器件的直接比较。',
        '本篇有风险结论仅针对综述中的性能比较和结构描述口径；引用编号、作者名单及页码错误不单独升级。',
        '（1）问题位置：第6页Table 1。问题类型：响应指标口径错误。发现：HASEL指标写成25 Hz；来源论文给出的两种手指带宽为15.9 Hz和8.34 Hz，25 Hz是共振区上界，不能直接当作同口径响应指标。为什么重要：会改变不同器件的性能比较。核查依据：对照Table 1及所引来源。\n（2）问题位置：第2页。问题类型：单位口径错误。发现：比功0.38 kJ/kg和比功率1.42 kW/kg被合并成一个指标，两个量纲应分开。为什么重要：读者会误把两个不同物理量当成同一指标。核查依据：对照第2页原文单位。\n（3）问题位置：第5页。问题类型：器件数量和自由度混淆。发现：4个压电手指、12个自由度被写成12个夹层驱动单元。为什么重要：结构数量和自由度的含义不同。核查依据：对照第5页正文及图示。图像核对未发现重复或拼接。',
        '研究设计与统计维度未发现独立风险；问题集中在综述表格和正文的指标、单位及对象转述。',
        [ev(ROOT/'韩有承/10.53941_ijamm.2026.100002/artifacts/ev_data2_p6_table1_row8.png','data',1,'问题1核心证据：第6页Table 1中的HASEL频率条目。'),ev(ROOT/'韩有承/10.53941_ijamm.2026.100002/artifacts/ev_data1_p2_cavatappi.png','data',2,'问题2核心证据：第2页比功与比功率的合并表述。'),ev(ROOT/'韩有承/10.53941_ijamm.2026.100002/artifacts/ev_data3a_p5_yuen_module.png','data',3,'问题3核心证据：第5页器件数量和自由度表述。')])
    ij['dimension_verdicts']={'image':'建议低风险','data':'建议高风险','ref':'建议低风险','methodology':'建议低风险'}
    ij['image_review']='图像维度：已核对正文图件，未发现图像重复、拼接或错配。'
    ij['ref_review']='参考文献维度：引用编号、作者名单和页码存在普通勘误，但不作为本篇有风险结论的独立依据。'
    final['韩有承'].append(ij)
    # Ensure deterministic ordering and write one array per scholar.
    for scholar, rows in final.items():
        # Do not let the renderer silently reuse the old detector evidence
        # bundles; this batch uses the adjudicated current-model findings.
        for row in rows:
            for key in ('_report_json','_input_dir','_coverage_evidence_path',
                        '_coverage_gaps','_coverage_status'):
                row.pop(key, None)
            bundle = {'doi': row.get('doi'), 'paper': {'title': row.get('doi')},
                      'main_figures': [], 'high_findings': [],
                      'deterministic_findings': {},
                      'review_guidance': '本轮采用当前模型逐篇核验后的读者可复核结论；截图路径随报告记录。'}
            ep = EVID / (row.get('doi','unknown').replace('/','_') + '.json')
            json.dump(bundle, ep.open('w'), ensure_ascii=False, indent=2)
            row['_evidence_path'] = str(ep)
        rows.sort(key=lambda x: x['doi'])
        p=OUT/f'{scholar}.json'; json.dump(rows,p.open('w'),ensure_ascii=False,indent=2)
        print(p, len(rows), sum(r['result']=='高风险' for r in rows))

if __name__ == '__main__': main()
