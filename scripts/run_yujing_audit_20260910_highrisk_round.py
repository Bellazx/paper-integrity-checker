#!/usr/bin/env python3
"""Three-stage review for initial high-risk papers in 20260910/SD and 非SD."""
from __future__ import annotations
import importlib.util, json, subprocess
from pathlib import Path
import pymssql

BASE=Path('/home/sjtu/paper-integrity-checker')
ROOTS=[BASE/'data/input/20260910/SD', BASE/'data/input/20260910/非SD']
INIT=BASE/'data/output/yujing_audit/init'
TEMPLATE=Path('/home/library/.claude/skills/paper-batch-review/scripts/yujing_audit_round_9')
ROUND_NAME='yujing_audit_20260910_highrisk_round_1'; ROUND_DIR=BASE/'data'/ROUND_NAME
ROUND_NUM=415; START_BATCH=1501000; EXPECTED=1172
APPLY_SCRIPT=BASE/'scripts/apply_yujing_audit_20260910_highrisk_round.py'
DB=dict(server='10.119.5.44',user='yujing',password='fengxian_YJ514',database='lunwenyujing',charset='utf8',timeout=30)

def report_doi(p):
    try:
        x=json.loads(p.read_text(encoding='utf-8')); return str((x.get('paper') or {}).get('doi') or '').strip()
    except Exception:return ''
def feed():
    bdir=ROUND_DIR/'batches'; bdir.mkdir(parents=True,exist_ok=True)
    if list(bdir.glob('batch_*.json')): raise RuntimeError('批次目录已有文件')
    unique={}; duplicates=[]
    for root in ROOTS:
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            rp=INIT/d.name/'report.json'; doi=report_doi(rp)
            if not doi or not rp.is_file(): raise RuntimeError(f'缺少报告 {d}')
            key=doi.lower()
            if key in unique: duplicates.append((str(d),str(unique[key][0]),doi))
            else: unique[key]=(d,rp)
    conn=pymssql.connect(**DB)
    try:
        cur=conn.cursor(as_dict=True); cur.execute("SELECT id,doi,report_url,status,risk_level FROM yujing_audit WHERE status=0 AND risk_level=N'高风险'")
        db={str(r['doi']).strip().lower():r for r in cur.fetchall()}
    finally: conn.close()
    selected=[(r,unique[k][0],unique[k][1]) for k,r in db.items() if k in unique]
    selected.sort(key=lambda x:int(x[0]['id']))
    if len(selected)!=EXPECTED: raise RuntimeError(f'高风险匹配数量异常: {len(selected)}')
    papers=[]
    for off,(r,d,rp) in enumerate(selected):
        bn=START_BATCH+off; first=rp.parent
        item={'id':int(r['id']),'doi':str(r['doi']).strip(),'slug':str(r['doi']).replace('/','_'),'source_path':str(d.parent.relative_to(BASE/'data')),
              'initial_report_url':r['report_url'],'input_dir':str(d.resolve()),'report_json':str(rp.resolve()),'first_check_dir':str(first.resolve()),
              'first_check_images':str(first/'images') if (first/'images').exists() else None,'first_check_data_tables':str(first/'data_tables') if (first/'data_tables').exists() else None,
              'evidence_path':str(ROUND_DIR/'evidence'/f'batch_{bn:04d}_evidence.json')}
        (bdir/f'batch_{bn:04d}.json').write_text(json.dumps([item],ensure_ascii=False,indent=2),encoding='utf-8'); papers.append(item)
    (ROUND_DIR/'papers.json').write_text(json.dumps(papers,ensure_ascii=False,indent=2),encoding='utf-8')
    (ROUND_DIR/'duplicate_materials.json').write_text(json.dumps(duplicates,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'feed complete: selected_initial_highrisk={len(papers)} duplicate_dirs={len(duplicates)}',flush=True)
def prompts():
    c=ROUND_DIR/'config'; c.mkdir(parents=True,exist_ok=True); out={}
    for stage,fn in [('r1','audit_ai_prompt.txt'),('r2','audit_ai_prompt_reviewer2.txt'),('judge','audit_ai_prompt_judge.txt')]:
        t=(TEMPLATE/fn).read_text(encoding='utf-8').replace('data/audit_round_9',f'data/{ROUND_NAME}').replace('round9','audit_20260910_highrisk_round1').replace('yujing_audit_round_9',ROUND_NAME)
        t=t.replace('本轮动态高风险队列','本批 20260910/SD 与 非SD 初审高风险论文队列')
        p=c/fn; p.write_text(t,encoding='utf-8'); out[stage]=p
    return out
def daemon(stage):
    fn={'r1':'audit_ai_daemon_round9.py','r2':'audit_ai_daemon_reviewer2_round9.py','judge':'audit_ai_daemon_judge_round9.py'}[stage]
    sp=importlib.util.spec_from_file_location(f'hr_{stage}',TEMPLATE/fn); m=importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
    m.ROUND_DIR=str(ROUND_DIR/'batches'); m.ROUND_NUM=ROUND_NUM; m.PATH_PAPER=str(BASE); m.STAGE=stage
    suf='' if stage=='r1' else f'_{stage}'
    m.LOG=str(ROUND_DIR/'monitors'/f'daemon{suf}.log'); m.TRACKER=str(ROUND_DIR/'monitors'/f'tracker{suf}.json'); m.LOG_DIR=str(ROUND_DIR/'monitors'/f'agent_logs{suf}')
    m.PROMPT_FILE=str(ROUND_DIR/'config'/({'r1':'audit_ai_prompt.txt','r2':'audit_ai_prompt_reviewer2.txt','judge':'audit_ai_prompt_judge.txt'}[stage]))
    if stage=='r1': m.RES_DIR=str(ROUND_DIR/'results')
    elif stage=='r2': m.RES1_DIR=str(ROUND_DIR/'results');m.RES2_DIR=str(ROUND_DIR/'results_reviewer2')
    else:
        m.RES1_DIR=str(ROUND_DIR/'results');m.RES2_DIR=str(ROUND_DIR/'results_reviewer2');m.RESJ_DIR=str(ROUND_DIR/'results_judge');m.APPLY_SCRIPT=str(APPLY_SCRIPT);m.APPLY_STAMP=str(ROUND_DIR/'monitors/apply_judge.stamp')
    Path(m.LOG_DIR).mkdir(parents=True,exist_ok=True); Path(m.TRACKER).parent.mkdir(parents=True,exist_ok=True)
    for a in ('RES_DIR','RES2_DIR','RESJ_DIR'):
        if hasattr(m,a): Path(getattr(m,a)).mkdir(parents=True,exist_ok=True)
    return m
def run(stage,target): prompts(); daemon(stage).main(target)
def launch(target):
    prompts(); md=ROUND_DIR/'monitors';md.mkdir(parents=True,exist_ok=True)
    for s in ('r1','r2','judge'):
        with (md/f'launcher_{s}.log').open('a',encoding='utf-8') as h: subprocess.Popen([str(BASE/'venv/bin/python'),str(Path(__file__).resolve()),s,'--target',str(target)],cwd=BASE,stdout=h,stderr=subprocess.STDOUT,start_new_session=True)
    print(f'launched target={target}',flush=True)
def main():
    import argparse
    p=argparse.ArgumentParser();p.add_argument('action',choices=('feed','launch','r1','r2','judge'));p.add_argument('--target',type=int,default=20);a=p.parse_args()
    if a.action=='feed': return feed()
    if a.action=='launch': return launch(a.target)
    return run(a.action,a.target)
if __name__=='__main__': raise SystemExit(main())
