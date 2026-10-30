#!/usr/bin/env python3
"""Render final judge reports and update yujing_audit for the 20260910/非SD round."""
from __future__ import annotations
import json, os, subprocess, sys
from datetime import datetime
from pathlib import Path
import pymssql
sys.path.insert(0,'/home/sjtu/paper-integrity-checker')
from modules.chinese_report_generator import doi_to_slug

BASE=Path('/home/sjtu/paper-integrity-checker'); ROUND_DIR=BASE/'data/yujing_audit_20260910_nonSD_round_1'
BATCH_DIR=ROUND_DIR/'batches'; JUDGE_DIR=ROUND_DIR/'results_judge'; APPLIED=ROUND_DIR/'monitors/applied.json'
PDF_DIR=BASE/'data/output/yujing_audit/review_v2'; REPORT=BASE/'scripts/generate_review_report.py'; PY=BASE/'venv/bin/python'
DB=dict(server='10.119.5.44',user='yujing',password='fengxian_YJ514',database='lunwenyujing',charset='utf8',timeout=30)

def norm(v):
    s=str(v or '').strip().lower()
    if s in ('low_risk','low risk') or '低' in str(v): return '低风险'
    if s in ('high_risk','high risk') or '高' in str(v): return '高风险'
    return str(v or '')
def load(path):
    d=json.loads(path.read_text(encoding='utf-8')); return d[0] if isinstance(d,list) else d
def done():
    try:return {int(x) for x in json.loads(APPLIED.read_text())}
    except:return set()
def save(s):
    APPLIED.parent.mkdir(parents=True,exist_ok=True); APPLIED.write_text(json.dumps(sorted(s),indent=2),encoding='utf-8')
def main():
    applied=done(); targets=[]
    for bp in sorted(BATCH_DIR.glob('batch_*.json')):
        bn=int(bp.stem.split('_')[1]); jp=JUDGE_DIR/f'batch_{bn:04d}.json'
        if bn not in applied and jp.exists(): targets.append((bn,load(bp),load(jp)))
    print(f'judge ready apply: {len(targets)}',flush=True)
    for i,(bn,p,j) in enumerate(targets,1):
        result=norm(j.get('result'))
        if result not in ('高风险','低风险'): raise RuntimeError(f'invalid result {bn}')
        item=dict(j); item.update({'doi':p['doi'],'result':result,'verdict':j.get('verdict',f'建议{result}'),'_input_dir':p.get('input_dir'),'_report_json':p.get('report_json'),'_evidence_path':p.get('evidence_path') or p.get('_evidence_path')})
        chunk=ROUND_DIR/'apply_chunks'/f'batch_{bn:04d}.json'; chunk.parent.mkdir(parents=True,exist_ok=True); chunk.write_text(json.dumps([item],ensure_ascii=False),encoding='utf-8')
        env=os.environ.copy(); env.update({'PAPER_INTEGRITY_ROOT':str(BASE),'PAPER_INTEGRITY_INPUT':str(BASE/'data/input'),'PAPER_INTEGRITY_OUTPUT':str(BASE/'data/output')})
        subprocess.run([str(PY),str(REPORT),'--results',str(chunk),'--output',str(PDF_DIR),'--namespace','yujing_audit','--table','yujing_audit','--no-db'],cwd=BASE,env=env,check=True)
        slug=doi_to_slug(p['doi']); pdf=PDF_DIR/f'review_{slug}.pdf'
        if not pdf.exists(): raise RuntimeError(f'missing pdf {pdf}')
        conn=pymssql.connect(**DB)
        try:
            cur=conn.cursor(); cur.execute('''UPDATE yujing_audit SET review_result=%s, review_report_url=%s, review_time=%s, status=2 WHERE id=%s AND doi=%s''',(result,f'http://202.120.57.100/review_reports/yujing_audit/review_{slug}.pdf',datetime.now(),p['id'],p['doi']))
            if cur.rowcount!=1: raise RuntimeError(f'db rows {cur.rowcount} id={p["id"]}')
            conn.commit()
        finally: conn.close()
        applied.add(bn); save(applied)
        if i%20==0 or i==len(targets): print(f'[{i}/{len(targets)}] {p["doi"]} -> {result}',flush=True)
    return 0
if __name__=='__main__': raise SystemExit(main())
