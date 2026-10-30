#!/usr/bin/env python3
"""Finish the explicitly selected 197 reviews; preserve scientific evidence gates."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import pymssql
from utils.db import DB_CONFIG

RD = BASE / 'data/yujing_audit_20261019_197_review_20261027'
SOURCE = BASE / 'data/output/yujing_audit/20261019_low_rerun_20261023_093247/manifest.json'
OLD = BASE / 'data/audit_round_9'
PY = BASE / 'venv/bin/python'
SKILL = Path('/home/library/.claude/skills/paper-batch-review')
NAMESPACE = 'yujing_audit_20261019_197_review_20261027'
PDF_DIR = BASE / 'data/output' / NAMESPACE / 'review_v2'


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def one(path):
    value = load(path)
    if isinstance(value, list):
        if len(value) != 1:
            raise ValueError(f'Expected exactly one paper: {path}')
        return value[0]
    return value


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    temporary.replace(path)


def prepare():
    if (RD / 'papers.json').exists():
        raise RuntimeError('Frozen manifest already exists; use run/status')
    original = load(SOURCE)
    assert original['papers_total'] == len(original['papers']) == 13621
    with pymssql.connect(**DB_CONFIG, timeout=30, login_timeout=15) as conn:
        cursor = conn.cursor(as_dict=True)
        cursor.execute('SELECT id,doi,risk_level,status,review_result,review_time,review_report_url,generation_time FROM yujing_audit')
        records = cursor.fetchall()
    by_id = {r['id']: r for r in records}
    doi_counts = {}
    for r in records:
        doi = str(r['doi'] or '').strip().lower()
        doi_counts[doi] = doi_counts.get(doi, 0) + 1
    matched = []
    for p in original['papers']:
        r = by_id[p['id']]
        assert r['doi'].lower() == p['doi'].lower(), p
        matched.append(r)
    assert sum(r['risk_level'] == '高风险' for r in matched) == 197
    assert sum(r['risk_level'] == '低风险' for r in matched) == 13424
    old_batches = {}
    for path in (OLD / 'batches').glob('batch_*.json'):
        p = one(path)
        old_batches[p['id']] = path
    selected = []
    for p in original['papers']:
        r = by_id[p['id']]
        if r['risk_level'] != '高风险':
            continue
        assert doi_counts[p['doi'].lower()] == 1, 'DOI-scoped writer requires unique DOI'
        source = Path(p['source']).resolve()
        report = BASE / 'data/output/yujing_audit/init' / p['paper'] / 'report.json'
        assert source.is_dir() and report.is_file(), p
        assert one(report)['paper']['doi'].lower() == p['doi'].lower(), p
        old = old_batches[p['id']]
        batch = int(old.stem.split('_')[1])
        item = {'id': p['id'], 'doi': p['doi'], 'batch': batch, 'input_dir': str(source),
                'report_json': str(report), 'initial_generation_time': str(r['generation_time']),
                'initial_report_sha256': hashlib.sha256(report.read_bytes()).hexdigest(),
                'evidence_path': str(RD / 'evidence' / f'{batch}.json'),
                'r1': str(OLD / 'results' / old.name),
                'r2': str(OLD / 'results_reviewer2' / old.name),
                'judge': str(OLD / 'results_judge' / old.name)}
        for field in ('r1', 'judge'):
            assert Path(item[field]).is_file(), item
            assert one(item[field])['doi'].lower() == p['doi'].lower(), item
        if one(item['r1']).get('result') == '低风险':
            assert Path(item['r2']).is_file(), item
        assert one(item['judge']).get('result') in ('高风险', '低风险'), item
        selected.append(item)
    save(RD / 'database_before.json', [by_id[p['id']] for p in selected])
    save(RD / 'papers.json', selected)
    for p in selected:
        save(RD / 'batches' / f"batch_{p['batch']}.json", [p])
    print(f'Frozen {len(selected)} papers; initial cohort=13621, high=197, low=13424', flush=True)


def command(args, log_path, *, timeout=3600, prompt=None, cwd=BASE):
    env = os.environ.copy()
    env.update(PAPER_INTEGRITY_ROOT=str(BASE), PAPER_INTEGRITY_INPUT=str(BASE / 'data/input'),
               PAPER_INTEGRITY_OUTPUT=str(BASE / 'data/output'), REVIEW_SKIP_REPORT_COVERAGE='0',
               OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1', NUMEXPR_NUM_THREADS='1')
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, 'a', encoding='utf-8') as log:
        proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE if prompt else subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, text=True)
        try:
            proc.communicate(input=prompt, timeout=timeout)
        except BaseException:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise
    return proc.returncode


def instructions(p, output, retry):
    return f'''完成一篇论文的最终复核与可发表报告内容整理。任务已获用户授权。
必读：{SKILL}/RUNTIME_RULES.md、{SKILL}/references/forensic_methods.md、
{SKILL}/references/image_false_positive_patterns.md。
输入清单：{RD}/batches/batch_{p['batch']}.json
正文/源数据目录：{p['input_dir']}
当前检测数据：{p['report_json']}
新建证据包：{p['evidence_path']}
已完成的第一轮复核：{p['r1']}
对抗复核（若存在）：{p['r2']}
已有最终判定：{p['judge']}

不是盲目重写已有结论。先通读论文相关正文/表注/附录和查看实际图像，再对已有判定的
每个关键问题核验。已有审核是线索而非事实。结合学科背景区分作者已解释的统计范围、
加权均值、独立运行、不同随机种子、双栏错序、正常拟合偏差等；严禁以缺少材料或
调用失败判有风险。不需重复已经有完整证据且复核成立的扫描，但应核对全部must_address。
若现有报告缺覆盖，实际检查后补写具体结论，不准虚构“已核查”。

输出简洁中文报告JSON对象（不是数组）到 {output}，以便运行预检。
字段：doi,result,verdict,trigger,image_review,data_review,ref_review,methodology_review,reason,
dimension_verdicts,evidence_images,_input_dir,_report_json,_evidence_path。
doi必须是 {p['doi']}。result只可高风险/低风险，verdict只可建议高风险/建议低风险；
dimension_verdicts必须含image,data,ref,methodology四项内部枚举。
_input_dir={p['input_dir']}；_report_json={p['report_json']}；_evidence_path={p['evidence_path']}。
公开文字仅使用“有风险／未发现风险”，禁止“原报告”“初审报告”“旧报告”和内部流程。
每个有风险维度用行首（1）（2）编号，每项按问题位置、问题类型、具体情况、需关注原因、
核查依据五标签，直说关键数值/图号/页码和有限影响；避免长篇量化取证过程。
确认问题依重要性排序，reason与各维度逐条一致，不得夸大为研究整体失效。
已排除项按类别、数量、具体理由精简，保留覆盖validator需要的事实，不可只写“均为假阳性”。
methodology_review也要有真实核查范围与结论，不能留空以至四维校验失败。

每个确认问题必须附实际查看过的对应核心局部截图，文字放图外，每个问题下面显示自己的图。
evidence_images项含dimension,issue_index（对应本维度编号）,caption,pdf_path,page,clip
或已保存图片path。必须渲染并亲眼核对clip，不能猜坐标。保留表头、单位、图号，严禁
图内覆写长段文字。证据图保存至 {RD}/evidence_images/{p['batch']}/，不要放临时目录。
相邻图颜色不同本身不算问题。确认没有实质问题则判未发现风险，不需要凑截图。

仅写本篇JSON与证据图片，不操作数据库、不生成最终PDF、不改源文件、不改共享代码。
输出后执行并修复如下校验（禁止修改/绕过校验器）：
{PY} {BASE}/scripts/validate_review_result.py --result {output} --evidence {p['evidence_path']} --doi '{p['doi']}'
校验器报错时逐项核查材料后修正措辞/编号/证据，不要仅拼凑关键词。
{retry}
结束只简述完成情况。
'''


def process(p):
    batch = p['batch']
    state_path = RD / 'state' / f'{batch}.json'
    output = RD / 'final' / f'{batch}.json'
    def state(stage, **extra):
        save(state_path, {'batch': batch, 'id': p['id'], 'doi': p['doi'], 'stage': stage,
                          'updated_at': datetime.now().isoformat(), **extra})
    previous = load(state_path) if state_path.exists() else {}
    if previous.get('stage') == 'complete':
        return load(state_path)
    try:
        state('evidence')
        evidence = Path(p['evidence_path'])
        evidence.parent.mkdir(parents=True, exist_ok=True)
        if not evidence.exists():
            rc = command([str(PY), str(SKILL / 'scripts/review_evidence.py'), '--doi', p['doi'],
                          '--input-dir', p['input_dir'], '--report-json', p['report_json'], '--out', str(evidence)],
                         RD / 'logs' / f'{batch}_evidence.log', timeout=900)
            if rc or not evidence.exists():
                raise RuntimeError(f'evidence build failed rc={rc}')
        workspace = RD / 'work' / str(batch)
        workspace.mkdir(parents=True, exist_ok=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        retry = ''
        # A scheduling restart can interrupt any unfinished stage. Resume its
        # paper-specific session instead of starting the scientific audit over.
        resume = bool(previous)
        for attempt in range(1, 4):
            state('final_check', attempt=attempt)
            prompt = instructions(p, output, retry)
            (RD / 'logs' / f'{batch}_{attempt}.prompt').write_text(prompt, encoding='utf-8')
            cli = ['claude', '--print', '--permission-mode', 'acceptEdits', '--add-dir', str(BASE)]
            if resume:
                session_root = Path.home() / '.claude/projects' / re.sub(r'[^a-zA-Z0-9]', '-', str(workspace))
                sessions = sorted(session_root.glob('*.jsonl'), key=lambda path: path.stat().st_mtime)
                if sessions:
                    cli.extend(['--resume', sessions[-1].stem])
                    prompt += '\n接续上次核查。保留已经完成的实质核验，只补缺口并输出最终JSON；不得跳过必查项或依据超时作风险判断。'
            try:
                rc = command(cli, RD / 'logs' / f'{batch}_{attempt}.log',
                             timeout=3600, prompt=prompt, cwd=workspace)
            except subprocess.TimeoutExpired:
                retry = '上次核查达到运行时限，请接续已有会话和证据，完成尚未核验项及最终JSON。'
                resume = True
                continue
            resume = True
            if rc or not output.exists():
                retry = f'上次未完成（退出码{rc}），请完成全部核查与JSON输出。'
                continue
            result = one(output)
            if result.get('doi', '').lower() != p['doi'].lower():
                raise RuntimeError('Output DOI does not match frozen selection')
            result.update(_input_dir=p['input_dir'], _report_json=p['report_json'], _evidence_path=p['evidence_path'])
            save(output, [result])
            # Render into an isolated namespace with no DB writes first.
            state('rendering', attempt=attempt, result=result.get('result'))
            log = RD / 'logs' / f'{batch}_{attempt}_render.log'
            rc = command([str(PY), str(BASE / 'scripts/generate_review_report.py'), '--results', str(output),
                          '--output', str(PDF_DIR), '--namespace', NAMESPACE, '--table', 'yujing_audit', '--no-db'],
                         log, timeout=600)
            if rc:
                retry = '上次最终PDF校验失败。请依据材料解决后重写JSON，不能改验证代码：\n' + log.read_text()[-5500:]
                continue
            from modules.chinese_report_generator import doi_to_slug
            import fitz
            pdf = PDF_DIR / f"review_{doi_to_slug(p['doi'])}.pdf"
            with fitz.open(pdf) as doc:
                if len(doc) == 0:
                    raise RuntimeError('Empty PDF')
                text = '\n'.join(page.get_text() for page in doc)
                if '\ufffd' in text:
                    raise RuntimeError('PDF contains replacement characters')
            # Render and publish through the common writer only after preflight.
            state('writing_database', attempt=attempt, result=result['result'])
            with pymssql.connect(**DB_CONFIG, timeout=30) as conn:
                c = conn.cursor(as_dict=True)
                c.execute('SELECT id,doi,risk_level,generation_time FROM yujing_audit WHERE doi=%s', (p['doi'],))
                rows = c.fetchall()
                if (len(rows) != 1 or rows[0]['id'] != p['id'] or rows[0]['risk_level'] != '高风险'
                        or str(rows[0]['generation_time']) != p['initial_generation_time']):
                    raise RuntimeError('DB scope or initial version changed since selection')
            if hashlib.sha256(Path(p['report_json']).read_bytes()).hexdigest() != p['initial_report_sha256']:
                raise RuntimeError('Initial evidence changed since selection')
            rc = command([str(PY), str(BASE / 'scripts/generate_review_report.py'), '--results', str(output),
                          '--output', str(PDF_DIR), '--namespace', NAMESPACE, '--table', 'yujing_audit'],
                         RD / 'logs' / f'{batch}_publish.log', timeout=600)
            if rc:
                raise RuntimeError(f'Publication failed rc={rc}; see publish log')
            state('complete', result=result['result'], pdf=str(pdf))
            return load(state_path)
        raise RuntimeError('Final report still failed validation after 3 evidence-based attempts; inspect logs')
    except Exception as exc:
        state('failed', error=f'{type(exc).__name__}: {exc}')
        return load(state_path)


def status():
    from collections import Counter
    states = [load(p) for p in (RD / 'state').glob('*.json')]
    summary = {'total': len(load(RD / 'papers.json')), 'updated_at': datetime.now().isoformat(),
               'stages': dict(Counter(s['stage'] for s in states)),
               'completed_results': dict(Counter(s['result'] for s in states if s['stage'] == 'complete'))}
    save(RD / 'status.json', summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def run(workers):
    RD.mkdir(parents=True, exist_ok=True)
    with (RD / 'run.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        save(RD / 'run.json', {'pid': os.getpid(), 'workers': workers, 'started_at': datetime.now().isoformat()})
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(process, p) for p in load(RD / 'papers.json')]
            for f in as_completed(futures):
                result = f.result()
                print(json.dumps(result, ensure_ascii=False), flush=True)
                status()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'run', 'status'))
    parser.add_argument('--workers', type=int, default=32)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare()
    elif args.action == 'run':
        run(args.workers)
    else:
        status()
