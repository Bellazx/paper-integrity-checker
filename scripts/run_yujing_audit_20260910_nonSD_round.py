#!/usr/bin/env python3
"""Three-stage batch review for yujing_audit papers under 20260910/非SD."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pymssql

BASE = Path('/home/sjtu/paper-integrity-checker')
INPUT_ROOT = BASE / 'data/input/20260910/非SD'
INIT_ROOT = BASE / 'data/output/yujing_audit/init'
TEMPLATE_DIR = Path('/home/library/.claude/skills/paper-batch-review/scripts/yujing_audit_round_9')
ROUND_NAME = 'yujing_audit_20260910_nonSD_round_1'
ROUND_DIR = BASE / 'data' / ROUND_NAME
ROUND_NUM = 414
START_BATCH = 1401000
EXPECTED_DIRS = 5644
EXPECTED = 5643
APPLY_SCRIPT = BASE / 'scripts/apply_yujing_audit_20260910_nonSD_round.py'
DB = dict(server='10.119.5.44', user='yujing', password='fengxian_YJ514',
          database='lunwenyujing', charset='utf8', timeout=30)


def load_daemon(stage: str):
    name = {'r1': 'audit_ai_daemon_round9.py', 'r2': 'audit_ai_daemon_reviewer2_round9.py',
            'judge': 'audit_ai_daemon_judge_round9.py'}[stage]
    spec = importlib.util.spec_from_file_location(f'nonSD_{stage}', TEMPLATE_DIR / name)
    if not spec or not spec.loader:
        raise RuntimeError(f'无法加载模板: {name}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROUND_DIR = str(ROUND_DIR / 'batches')
    mod.ROUND_NUM = ROUND_NUM
    mod.PATH_PAPER = str(BASE)
    mod.STAGE = stage
    suffix = '' if stage == 'r1' else f'_{stage}'
    mod.LOG = str(ROUND_DIR / 'monitors' / f'daemon{suffix}.log')
    mod.TRACKER = str(ROUND_DIR / 'monitors' / f'tracker{suffix}.json')
    mod.LOG_DIR = str(ROUND_DIR / 'monitors' / f'agent_logs{suffix}')
    mod.PROMPT_FILE = str(ROUND_DIR / 'config' / ({'r1':'audit_ai_prompt.txt','r2':'audit_ai_prompt_reviewer2.txt','judge':'audit_ai_prompt_judge.txt'}[stage]))
    if stage == 'r1':
        mod.RES_DIR = str(ROUND_DIR / 'results')
    elif stage == 'r2':
        mod.RES1_DIR = str(ROUND_DIR / 'results'); mod.RES2_DIR = str(ROUND_DIR / 'results_reviewer2')
    else:
        mod.RES1_DIR = str(ROUND_DIR / 'results'); mod.RES2_DIR = str(ROUND_DIR / 'results_reviewer2')
        mod.RESJ_DIR = str(ROUND_DIR / 'results_judge')
        mod.APPLY_SCRIPT = str(APPLY_SCRIPT)
        mod.APPLY_STAMP = str(ROUND_DIR / 'monitors' / 'apply_judge.stamp')
    for attr, path in [('RES_DIR', ROUND_DIR/'results'), ('RES2_DIR', ROUND_DIR/'results_reviewer2'), ('RESJ_DIR', ROUND_DIR/'results_judge')]:
        if hasattr(mod, attr): Path(path).mkdir(parents=True, exist_ok=True)
    Path(mod.LOG_DIR).mkdir(parents=True, exist_ok=True)
    Path(mod.TRACKER).parent.mkdir(parents=True, exist_ok=True)
    return mod


def log(msg: str) -> None:
    p = ROUND_DIR / 'monitors/feeder.log'; p.parent.mkdir(parents=True, exist_ok=True)
    print(msg, flush=True)
    with p.open('a', encoding='utf-8') as f: f.write(msg + '\n')


def load_report_doi(path: Path) -> str:
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
        return str((d.get('paper') or {}).get('doi') or '').strip()
    except Exception:
        return ''


def feed() -> int:
    bdir = ROUND_DIR / 'batches'
    if list(bdir.glob('batch_*.json')):
        raise RuntimeError(f'批次目录已有文件: {bdir}')
    bdir.mkdir(parents=True, exist_ok=True)
    items = []
    for d in sorted(p for p in INPUT_ROOT.iterdir() if p.is_dir()):
        report = INIT_ROOT / d.name / 'report.json'
        doi = load_report_doi(report)
        if not doi or not report.is_file():
            raise RuntimeError(f'缺少可匹配报告: {d}')
        items.append((d, report, doi))
    if len(items) != EXPECTED_DIRS:
        raise RuntimeError(f'输入目录数量异常: {len(items)}')
    # One download directory is a byte-identical duplicate of another
    # (10.1038_pcan.2015.57 contains the same PDF/report DOI as
    # 10.1038_pcan.2010.22).  A database row can only be reviewed once.
    unique_items = {}
    duplicate_dirs = []
    for item in items:
        key = item[2].lower()
        if key in unique_items:
            duplicate_dirs.append((item[0].name, unique_items[key][0].name, item[2]))
        else:
            unique_items[key] = item
    if len(unique_items) != EXPECTED:
        raise RuntimeError(f'唯一论文数量异常: {len(unique_items)}，重复目录={duplicate_dirs[:3]}')
    items = list(unique_items.values())
    conn = pymssql.connect(**DB)
    try:
        cur = conn.cursor()
        rows = []
        for d, report, doi in items:
            cur.execute('SELECT id,doi,report_url,status FROM yujing_audit WHERE doi=%s', (doi,))
            got = cur.fetchall()
            if len(got) != 1:
                raise RuntimeError(f'DB匹配异常 doi={doi} rows={len(got)}')
            rows.append((int(got[0][0]), str(got[0][1]).strip(), got[0][2], got[0][3], d, report))
    finally:
        conn.close()
    rows.sort(key=lambda x: x[0])
    papers = []
    for off, (pid, doi, report_url, status, d, report) in enumerate(rows):
        bn = START_BATCH + off
        first = report.parent
        item = {'id': pid, 'doi': doi, 'slug': doi.replace('/', '_'), 'source_path': 'input/20260910/非SD/',
                'initial_report_url': report_url, 'input_dir': str(d.resolve()), 'report_json': str(report.resolve()),
                'first_check_dir': str(first.resolve()),
                'first_check_images': str(first/'images') if (first/'images').exists() else None,
                'first_check_data_tables': str(first/'data_tables') if (first/'data_tables').exists() else None,
                'evidence_path': str(ROUND_DIR/'evidence'/f'batch_{bn:04d}_evidence.json')}
        (bdir/f'batch_{bn:04d}.json').write_text(json.dumps([item],ensure_ascii=False,indent=2),encoding='utf-8')
        papers.append(item)
    (ROUND_DIR/'papers.json').write_text(json.dumps(papers,ensure_ascii=False,indent=2),encoding='utf-8')
    if duplicate_dirs:
        (ROUND_DIR/'duplicate_materials.json').write_text(json.dumps(duplicate_dirs,ensure_ascii=False,indent=2),encoding='utf-8')
    log(f'feed complete: input_dirs={EXPECTED_DIRS} duplicate_dirs={len(duplicate_dirs)} db_matched={len(papers)} batches={len(papers)} status0_expected={sum(x[3] == 0 for x in rows)}')
    return 0


def prompts() -> dict[str, Path]:
    cdir = ROUND_DIR/'config'; cdir.mkdir(parents=True,exist_ok=True)
    out = {}
    for stage, fn in [('r1','audit_ai_prompt.txt'),('r2','audit_ai_prompt_reviewer2.txt'),('judge','audit_ai_prompt_judge.txt')]:
        text = (TEMPLATE_DIR/fn).read_text(encoding='utf-8')
        text = text.replace('data/audit_round_9', f'data/{ROUND_NAME}')
        text = text.replace('round9', 'audit_20260910_nonSD_round1')
        text = text.replace('yujing_audit_round_9', ROUND_NAME)
        text = text.replace('动态高风险队列', '20260910/非SD目录的完整论文队列')
        target = cdir/fn; target.write_text(text,encoding='utf-8'); out[stage]=target
    return out


def run_stage(stage: str, target: int) -> int:
    prompts(); mod = load_daemon(stage); mod.main(target); return 0


def launch(target: int) -> int:
    prompts(); md = ROUND_DIR/'monitors'; md.mkdir(parents=True,exist_ok=True)
    for stage in ('r1','r2','judge'):
        cmd = [str(BASE/'venv/bin/python'), str(Path(__file__).resolve()), stage, '--target', str(target)]
        with (md/f'launcher_{stage}.log').open('a',encoding='utf-8') as h:
            subprocess.Popen(cmd,cwd=BASE,stdout=h,stderr=subprocess.STDOUT,start_new_session=True)
    log(f'launched stages target={target}')
    return 0


def main() -> int:
    import argparse
    ap=argparse.ArgumentParser(); ap.add_argument('action',choices=('feed','launch','r1','r2','judge')); ap.add_argument('--target',type=int,default=20)
    a=ap.parse_args()
    if a.action=='feed': return feed()
    if a.action=='launch': return launch(a.target)
    return run_stage(a.action,a.target)

if __name__ == '__main__': raise SystemExit(main())
