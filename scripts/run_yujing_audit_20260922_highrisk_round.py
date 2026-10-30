#!/usr/bin/env python3
"""Three-stage review for 20260922 yujing_audit papers initially marked high risk."""
from __future__ import annotations
import importlib.util, json, subprocess
from pathlib import Path
import pymssql

BASE = Path('/home/sjtu/paper-integrity-checker')
INPUT_ROOT = BASE / 'data/input/20260922'
INIT_ROOT = BASE / 'data/output/yujing_audit/init'
TEMPLATE_DIR = Path('/home/library/.claude/skills/paper-batch-review/scripts/yujing_audit_round_9')
ROUND_NAME = 'yujing_audit_20260922_highrisk_round_1'
ROUND_DIR = BASE / 'data' / ROUND_NAME
ROUND_NUM = 416
START_BATCH = 1601000
APPLY_SCRIPT = BASE / 'scripts/apply_yujing_audit_20260922_highrisk_round.py'
DB = dict(server='10.119.5.44', user='yujing', password='fengxian_YJ514',
          database='lunwenyujing', charset='utf8', timeout=30)

def load_report_doi(path: Path) -> str:
    try:
        obj = json.loads(path.read_text(encoding='utf-8'))
        return str((obj.get('paper') or {}).get('doi') or '').strip()
    except Exception:
        return ''

def feed() -> int:
    bdir = ROUND_DIR / 'batches'
    if list(bdir.glob('batch_*.json')):
        raise RuntimeError(f'批次目录已有文件: {bdir}')
    bdir.mkdir(parents=True, exist_ok=True)
    all_items = []
    missing_doi = []
    for d in sorted(p for p in INPUT_ROOT.iterdir() if p.is_dir()):
        rp = INIT_ROOT / d.name / 'report.json'
        doi = load_report_doi(rp) if rp.is_file() else ''
        if not doi:
            missing_doi.append(d.name)
            continue
        all_items.append((d, rp, doi))
    unique = {}
    duplicate_dirs = []
    for item in all_items:
        key = item[2].lower()
        if key in unique:
            duplicate_dirs.append((item[0].name, unique[key][0].name, item[2]))
        else:
            unique[key] = item
    conn = pymssql.connect(**DB)
    try:
        cur = conn.cursor(as_dict=True)
        dois = list(unique)
        rows = {}
        for i in range(0, len(dois), 500):
            part = dois[i:i+500]
            cur.execute('SELECT id,doi,report_url,status,risk_level FROM yujing_audit WHERE doi IN (' + ','.join(['%s']*len(part)) + ')', part)
            for r in cur.fetchall():
                rows[str(r['doi']).strip().lower()] = r
    finally:
        conn.close()
    selected = []
    unmatched = []
    not_high = []
    for key, (d, rp, doi) in unique.items():
        r = rows.get(key)
        if not r:
            unmatched.append((d.name, doi))
        elif int(r['status']) == 0 and str(r['risk_level']) == '高风险':
            selected.append((r, d, rp))
        else:
            not_high.append((d.name, doi, r['status'], r['risk_level']))
    selected.sort(key=lambda x: int(x[0]['id']))
    papers = []
    for off, (r, d, rp) in enumerate(selected):
        bn = START_BATCH + off
        first = rp.parent
        item = {
            'id': int(r['id']), 'doi': str(r['doi']).strip(),
            'slug': str(r['doi']).replace('/', '_'),
            'source_path': 'input/20260922/',
            'initial_report_url': r['report_url'],
            'input_dir': str(d.resolve()), 'report_json': str(rp.resolve()),
            'first_check_dir': str(first.resolve()),
            'first_check_images': str(first/'images') if (first/'images').exists() else None,
            'first_check_data_tables': str(first/'data_tables') if (first/'data_tables').exists() else None,
            'evidence_path': str(ROUND_DIR/'evidence'/f'batch_{bn:04d}_evidence.json'),
        }
        (bdir/f'batch_{bn:04d}.json').write_text(json.dumps([item], ensure_ascii=False, indent=2), encoding='utf-8')
        papers.append(item)
    (ROUND_DIR/'papers.json').write_text(json.dumps(papers, ensure_ascii=False, indent=2), encoding='utf-8')
    (ROUND_DIR/'missing_doi.json').write_text(json.dumps(missing_doi, ensure_ascii=False, indent=2), encoding='utf-8')
    (ROUND_DIR/'unmatched.json').write_text(json.dumps(unmatched, ensure_ascii=False, indent=2), encoding='utf-8')
    (ROUND_DIR/'not_high_or_done.json').write_text(json.dumps(not_high, ensure_ascii=False, indent=2), encoding='utf-8')
    (ROUND_DIR/'duplicate_materials.json').write_text(json.dumps(duplicate_dirs, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'feed complete: input_dirs={len(list(INPUT_ROOT.iterdir()))} selected_initial_highrisk={len(papers)} missing_doi={len(missing_doi)} unmatched={len(unmatched)} duplicate_dirs={len(duplicate_dirs)} not_high_or_done={len(not_high)}', flush=True)
    return 0

def load_daemon(stage: str):
    filename = {'r1':'audit_ai_daemon_round9.py', 'r2':'audit_ai_daemon_reviewer2_round9.py', 'judge':'audit_ai_daemon_judge_round9.py'}[stage]
    spec = importlib.util.spec_from_file_location(f'audit20260922_{stage}', TEMPLATE_DIR/filename)
    mod = importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(mod)
    mod.ROUND_DIR = str(ROUND_DIR/'batches'); mod.ROUND_NUM = ROUND_NUM; mod.PATH_PAPER = str(BASE); mod.STAGE = stage
    suffix = '' if stage == 'r1' else f'_{stage}'
    mod.LOG = str(ROUND_DIR/'monitors'/f'daemon{suffix}.log')
    mod.TRACKER = str(ROUND_DIR/'monitors'/f'tracker{suffix}.json')
    mod.LOG_DIR = str(ROUND_DIR/'monitors'/f'agent_logs{suffix}')
    mod.PROMPT_FILE = str(ROUND_DIR/'config'/({'r1':'audit_ai_prompt.txt','r2':'audit_ai_prompt_reviewer2.txt','judge':'audit_ai_prompt_judge.txt'}[stage]))
    if stage == 'r1': mod.RES_DIR = str(ROUND_DIR/'results')
    elif stage == 'r2': mod.RES1_DIR = str(ROUND_DIR/'results'); mod.RES2_DIR = str(ROUND_DIR/'results_reviewer2')
    else:
        mod.RES1_DIR = str(ROUND_DIR/'results'); mod.RES2_DIR = str(ROUND_DIR/'results_reviewer2'); mod.RESJ_DIR = str(ROUND_DIR/'results_judge')
        mod.APPLY_SCRIPT = str(APPLY_SCRIPT); mod.APPLY_STAMP = str(ROUND_DIR/'monitors/apply_judge.stamp')
    for attr in ('RES_DIR','RES2_DIR','RESJ_DIR'):
        if hasattr(mod, attr): Path(getattr(mod, attr)).mkdir(parents=True, exist_ok=True)
    Path(mod.LOG_DIR).mkdir(parents=True, exist_ok=True); Path(mod.TRACKER).parent.mkdir(parents=True, exist_ok=True)
    return mod

def prompts() -> None:
    cdir = ROUND_DIR/'config'; cdir.mkdir(parents=True, exist_ok=True)
    for stage, fn in [('r1','audit_ai_prompt.txt'), ('r2','audit_ai_prompt_reviewer2.txt'), ('judge','audit_ai_prompt_judge.txt')]:
        text = (TEMPLATE_DIR/fn).read_text(encoding='utf-8')
        text = text.replace('data/audit_round_9', f'data/{ROUND_NAME}').replace('round9', 'audit_20260922_highrisk_round1').replace('yujing_audit_round_9', ROUND_NAME)
        text = text.replace('动态高风险队列', '20260922目录初审高风险论文队列')
        (cdir/fn).write_text(text, encoding='utf-8')

def run_stage(stage: str, target: int) -> int:
    prompts(); load_daemon(stage).main(target); return 0

def launch(target: int) -> int:
    prompts(); md = ROUND_DIR/'monitors'; md.mkdir(parents=True, exist_ok=True)
    for stage in ('r1','r2','judge'):
        with (md/f'launcher_{stage}.log').open('a', encoding='utf-8') as h:
            subprocess.Popen([str(BASE/'venv/bin/python'), str(Path(__file__).resolve()), stage, '--target', str(target)], cwd=BASE, stdout=h, stderr=subprocess.STDOUT, start_new_session=True)
    print(f'launched stages target={target}', flush=True); return 0

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument('action', choices=('feed','launch','r1','r2','judge')); ap.add_argument('--target', type=int, default=20)
    a = ap.parse_args()
    if a.action == 'feed': return feed()
    if a.action == 'launch': return launch(a.target)
    return run_stage(a.action, a.target)

if __name__ == '__main__': raise SystemExit(main())
