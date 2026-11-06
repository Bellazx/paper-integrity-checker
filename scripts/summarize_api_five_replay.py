#!/usr/bin/env python3
"""Summarize real API replays and their saved morning baseline."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys


def read(path): return json.loads(path.read_text())
def seconds(value): return datetime.fromisoformat(value).timestamp()
def pdf_info(path):
    import fitz
    with fitz.open(path) as doc:
        text = '\n'.join(p.get_text() for p in doc)
        images = set()
        for page in doc:
            for img in page.get_images():
                images.add(hashlib.sha256(doc.extract_image(img[0])['image']).hexdigest())
        return {'pages':len(doc),'text_chars':len(text),'image_hashes':sorted(images),
                'unchecked_mentions':text.count('未能完成核对'),'path':str(path)}

def review_info(task_path):
    p = task_path / 'review_results.json'
    if not p.exists(): return {}
    result = read(p)[0]
    info = {k:result.get(k) for k in ('result','trigger','_review_metrics','_coverage_status','_coverage_gaps',
                                     '_report_source','_unfinished_items')}
    info['evidence_images'] = len(result.get('evidence_images') or [])
    info['excel_regions'] = len(result.get('excel_evidence_regions') or [])
    work = result.get('_review_workdir')
    if work:
        start_match = re.match(r'run_(\d{8}_\d{6})_', Path(work).name)
        shards = Path(work)/'shards.json'
        if start_match and shards.exists():
            began = datetime.strptime(start_match[1], '%Y%m%d_%H%M%S').timestamp()
            before_sessions = max(0, shards.stat().st_mtime - began)
            info['pre_investigation_seconds_approx'] = round(before_sessions, 1)
            prepare = ((result.get('_review_metrics') or {}).get('timeline') or {}).get('prepare', 0)
            info['evidence_prepare_seconds_approx'] = round(max(0, before_sessions-prepare), 1)
        session_info = []
        session_errors = Counter()
        for p in (Path(work)/'sessions').glob('*.json'):
            d = read(p)
            session_errors.update(str(m['error']) for m in d.get('metrics',[]) if m.get('error'))
            session_info.append({'name':p.stem, 'stop_reason':d.get('stop_reason'),
                                 'chunks':len(d.get('metrics') or []),
                                 'wall_seconds':round(sum(m.get('wall',0) for m in d.get('metrics',[])),1)})
        info['sessions'] = session_info
        info['session_error_counts'] = dict(session_errors)
    if work and (Path(work)/'finals.json').exists():
        finals = read(Path(work)/'finals.json')['finals']
        info['statuses'] = dict(Counter(r.get('status') for r in finals.values()))
        info['visual_pass'] = finals.get('image::visual_pass')
        info['unresolved'] = [{'id':k,'title':v.get('title'),'note':v.get('note')} for k,v in finals.items() if v.get('status') in ('open','unresolved')]
        info['capture_checkpoint'] = (Path(work)/'capture_checkpoint.json').exists()
        info['sheet_cache_files'] = len(list((Path(work)/'dossier/workbooks').glob('*.jsonl')))
    return info

def main(job):
    manifest=read(job/'manifest.json'); current=read(job/'status.json')
    baseline=Path(manifest['baseline_source'])
    lines=(baseline.parent/'logs/api.log').read_text().splitlines()
    all_info={}
    for meta in manifest['papers']:
        key=meta['key']; task=meta['source_task']
        record=read(baseline/'data/tasks'/task/'status.json')
        total=seconds(record['completed_at'])-seconds(record['created_at'])
        def at(cue):
            for line in lines:
                if task in line and cue in line and re.match(r'\d{2}:\d{2}:\d{2}',line):
                    return datetime.fromisoformat('2026-11-06T'+line[:8]).timestamp()
            return None
        started=at('Detection cmd:'); review_start=at('English initial report generated:')
        report_start=at('Generating review reports:')
        report_end=at('English review report generated:')
        b={'total_seconds':round(total,3),'detection_seconds':review_start-started,
           'review_seconds':report_start-review_start,'report_seconds':report_end-report_start,
           'task':task,'review':review_info(baseline/'data/tasks'/task)}
        c=current['papers'].get(key,{})
        if c.get('task_id'):
            c={**c,'review_details':review_info(job/'snapshot/data/tasks'/c['task_id'])}
        for item,root in [(b,baseline),(c,job/'snapshot')]:
            taskid=item.get('task') or item.get('task_id')
            directory=root/'data/output/review_v2/detection_reports'/str(taskid)
            pdfs={('en' if p.stem.endswith('_en') else 'zh'):pdf_info(p) for p in directory.glob('*.pdf')}
            item['review_pdfs']=pdfs
            if len(pdfs)==2:
                item['bilingual_evidence_equal']=pdfs['en']['image_hashes']==pdfs['zh']['image_hashes']
        all_info[key]={'baseline':b,'current':c,'user_baseline_seconds':meta['baseline_seconds']}
    (job/'comparison.json').write_text(json.dumps(all_info,ensure_ascii=False,indent=2))
    def fmt(v):
        if v is None:return '运行中'
        return f'{int(round(v))//60}:{int(round(v))%60:02d}'
    out=['# 五篇 API 完整复跑', '',f"状态：{current['state']}。本轮使用当前代码快照、上午同一批输入副本，五篇并发、双语输出、75 分钟总预算。",'',
         '|论文|上午全程|本轮初检含双语|本轮复审|本轮双语报告|本轮全程|状态|',
         '|---|---:|---:|---:|---:|---:|---|']
    for key,data in all_info.items():
        b,c=data['baseline'],data['current']
        out.append('|'+ '|'.join([key,fmt(b['total_seconds']),fmt(c.get('detection_seconds')),fmt(c.get('review_seconds')),fmt(c.get('report_seconds')),fmt(c.get('total_seconds')),c.get('state','待开始')])+'|')
    out += ['', '计时口径：上午全程来自任务创建至完成时间；分阶段日志精确到秒。本轮计时来自 monotonic 时钟，初检包含初检报告翻译。测试直接调用真实 API worker，未覆盖 HTTP 上传、生产数据库持久化和公网下载。', '',
            '输入逐文件校验一致；全新初检、复审目录，不复用旧结论。固定代码与配置记录在 manifest.json。模型调用存在随机波动，且共享服务器有其他任务。当前评级规则还包含已有的 judge 更新，因此单轮比较不能独立归因于某一项性能修改。', '',
            '详细阶段、未完成项、截图与双语图片一致性见 comparison.json。']
    (job/'REPORT.md').write_text('\n'.join(out)+'\n')
    print('\n'.join(out[:12]))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('job',type=Path)
    main(p.parse_args().job.resolve())
