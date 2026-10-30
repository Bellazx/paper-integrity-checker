#!/usr/bin/env python3
"""Recover failed comparison papers; publish and commit only isolated tables."""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from datetime import datetime

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
os.environ['REVIEW_MODEL'] = 'MiniMax-M3.1-Flash'
os.environ['REVIEW_FALLBACK_MODEL'] = 'MiniMax-M3.1-Flash'

import pymssql
from api.services import review, review_cli
from api.services.review_validation import validate
from api.services.report_access import _check as check_public_pdf
from api.services.review_runtime import paper_deadline
from modules.chinese_report_generator import doi_to_slug
from utils.db import DB_CONFIG

NAMES = ('yujing_scholar_m31_compare_20261030',
         'yujing_scholar_m31_compare_20261030_additional')


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def save(path, value):
    scratch = path.with_suffix(path.suffix + '.tmp')
    scratch.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    scratch.replace(path)


def substantive(value):
    return value.get('trigger') != 'review_error' and value.get('result') in {'高风险', '低风险'}


def valid(value):
    if not substantive(value):
        return False
    _, gaps = validate(value, load(Path(value['_evidence_path'])))
    return not gaps


def attach_saved_evidence(root, value):
    if value.get('_evidence_path'):
        return value
    for path in (root / 'review_artifacts').glob('*_state.json'):
        state = load(path)
        identity = state.get('identity', {})
        if (identity.get('doi', '').casefold() == value['doi'].casefold()
                and identity.get('input', {}).get('path') == value.get('_input_dir')
                and identity.get('report', {}).get('path') == value.get('_report_json')):
            evidence = state.get('stages', {}).get('evidence_path')
            if evidence and Path(evidence).is_file():
                return {**value, '_evidence_path': evidence}
    raise ValueError(f'No matching isolated evidence checkpoint for {value["doi"]}')


def report_url(name, doi):
    return f'http://202.120.57.100/review_reports/{name}/review_{doi_to_slug(doi)}.pdf'


def publish(name, results, tag):
    """Errors remain pending; valid unrelated papers can be independently committed."""
    if name not in NAMES:
        raise ValueError('Only explicitly scoped temporary comparison tables may be written')
    root = BASE / 'data/output' / name
    snapshot = root / f'publication_{tag}.json'
    save(snapshot, results)
    env = os.environ.copy()
    env['PAPER_INTEGRITY_ROOT'] = str(BASE)
    env['REVIEW_COVERAGE_GAP_POLICY'] = 'strict'
    cmd = [str(BASE / 'venv/bin/python'), str(BASE / 'scripts/generate_review_report.py'),
           '--results', str(snapshot), '--output', str(root / 'review_v2'),
           '--table', name, '--namespace', name, '--no-db']
    log_path = root / f'publication_{tag}.log'
    with log_path.open('w', encoding='utf-8') as stream:
        proc = subprocess.run(cmd, cwd=BASE, env=env, stdout=stream, stderr=subprocess.STDOUT)
    if proc.returncode not in (0, 2):
        raise RuntimeError(f'Publication failed rc={proc.returncode}; see {log_path}')
    failure_path = snapshot.with_suffix('.publication_failures.json')
    failures = {item['doi']: item['error'] for item in load(failure_path)}
    conn = pymssql.connect(**DB_CONFIG, login_timeout=10, timeout=60)
    statuses = {}
    try:
        cur = conn.cursor(as_dict=True)
        for value in results:
            doi = value['doi']
            status = 'review_error' if not substantive(value) else 'report_pending'
            url = None
            if doi not in failures and valid(value):
                pdf = root / 'review_v2' / f'review_{doi_to_slug(doi)}.pdf'
                if pdf.is_file() and pdf.read_bytes()[:5] == b'%PDF-':
                    try:
                        check_public_pdf(report_url(name, doi))
                        status, url = 'completed', report_url(name, doi)
                    except (OSError, RuntimeError) as exc:
                        failures[doi] = str(exc)
            cur.execute(f'''UPDATE [{name}] SET comparison_run_id=%s,
                source_review_result=COALESCE(source_review_result,review_result),
                source_review_time=COALESCE(source_review_time,review_time),
                source_review_report_url=COALESCE(source_review_report_url,review_report_url),
                m31_model=%s,m31_review_result=%s,m31_review_report_url=%s,
                m31_review_time=%s,m31_result_json=%s,comparison_status=%s WHERE doi=%s''',
                (name, 'MiniMax-M3.1-Flash', value.get('result') if substantive(value) else None,
                 url, datetime.now() if status == 'completed' else None,
                 json.dumps(value, ensure_ascii=False), status, doi))
            cur.execute(f'SELECT comparison_status,m31_review_report_url FROM [{name}] WHERE doi=%s', (doi,))
            rows = cur.fetchall()
            if len(rows) != 1 or rows[0]['comparison_status'] != status or rows[0]['m31_review_report_url'] != url:
                raise RuntimeError(f'Temporary-table verification failed: {doi}')
            conn.commit()
            statuses[doi] = status
            print(f'Published {name} {doi}: {status}', flush=True)
    finally:
        conn.close()
    save(root / f'publication_{tag}_status.json', {'statuses': statuses, 'failures': failures})
    return statuses


async def recover(name, original):
    root = BASE / 'data/output' / name
    doi = original['doi']
    state_path = Path(original['_evidence_path'].replace('_evidence.json', '_state.json'))
    state = load(state_path)
    stages = state['stages']
    checkpoint = root / 'review_artifacts' / f'm31_recovery_v2_{doi_to_slug(doi)}.json'
    prior = load(checkpoint) if checkpoint.exists() else {}
    evidence_path = original['_evidence_path']
    ep = review.EvidencePaths(stages.get('llm_evidence_path') or evidence_path, evidence_path)
    candidates = {k: stages[k] for k in ('reviewer1', 'reviewer2', 'synthesis_candidate', 'coverage_repair') if k in stages}
    target = {
        '10.1109/IGARSS52108.2023.10282407':
            '两份实质结论有分歧。仅复核Fig.3(b)曲线、12.1dB阈值、MDV定义与正文适用范围：'
            '29.7–41.7m/s低于阈值是否否定5.2m/s最小可检测速度，还是属于盲速而非最小速度？'
            '核查均匀102.4及优化5.2是否依同一取值口径，并结合雷达领域背景。'
            '不要因普通图号/标题笔误升级。需要证据判断，不可采用任一候选作为默认。',
        '10.1126/science.adw8151':
            '已有综合草稿，定点读Fig.S2和Fig.S6图注以确认是否真有性别样本数著录矛盾、'
            '其影响及统计口径，不能混淆mean/median、SD/SEM。主要发布缺口：'
            'data_review仅5个顶层问题，第5下有两个子项，截图issue_index=6无对应项。'
            '可将两个子项编号为（5）（6），或把两张截图都准确绑定第5；保留事实与证据。',
        '10.3760/cma.j.cn116031.2024.1000158':
            '既有核查描述缺有效result/verdict输出。阅读保存的两份意见，必要时只核验争议的'
            '表1与正文90%/87%、血管/图像计数、OFR/QFR标签、综述适用范围。'
            '正文/图表可解释近似、引用编号、出版模板及格式不自行升级；'
            '具体笔误仍可记录，严重影响比较对象/数值的笔误可支持有风险。'
            '不能把缺输出、工具或流程错误当作风险。',
    }[doi]
    prompt = (
        f'对一篇M3.1配对实验论文做最终综合核验，DOI：{doi}。\n'
        f'论文材料目录：{original["_input_dir"]}\n证据包：{evidence_path}\n'
        '这是已完成广泛调查的失败项恢复，不重跑证据扫描、不重查所有文献。'
        '不得修改审核代码、证据包、数据库或输入论文。只核查未解决的具体项。\n'
        + target + '\n完整既有意见：\n' + json.dumps(candidates, ensure_ascii=False)
        + '\n最终JSON保留已核查四维事实及对应图号、数值与材料限制；不得捏造核验。'
        '文本应简练通俗，确认问题使用（1）（2）编号及问题位置/问题类型/具体情况/'
        '需关注原因/核查依据，每个截图dimension、issue_index必须对应该维度问题编号。'
        '只使用已存在的精准原文截图，普通非风险内容不要堆截图。'
        '公开文本使用有风险/未发现风险，不写原报告、初审报告、本人仲裁或审核过程。'
        '内部字段result高风险/低风险，verdict建议高风险/建议低风险，'
        'dimension_verdicts包含image/data/ref/methodology各自明确结论。'
        '总体结论与每维度、reason一致。无源数据本身不是风险。'
        + review._publication_preflight_instruction(doi, ep)
    )
    token = paper_deadline.set(time.monotonic() + 2100)
    try:
        result = await review_cli.run(review, doi, 'M31 targeted recovery', prompt, 24,
                                      state=prior, persist=lambda value: save(checkpoint, value))
    finally:
        paper_deadline.reset(token)
    result = {**result, **{k: v for k, v in original.items() if k.startswith('_') and k not in ('_review_error', '_retry_exhausted')}}
    result['_recovery_model'] = 'MiniMax-M3.1-Flash'
    result['_recovered_at'] = datetime.now().isoformat()
    if substantive(result):
        result, gaps = validate(result, load(Path(evidence_path)))
        if gaps:
            result = {**original, '_recovery_gaps': gaps}
        else:
            stages['comparison_recovery'] = result
            stages['judge'] = result
            stages['final'] = result
            stages['decision'] = {'flow':'targeted_comparison_recovery',
                                  'model':'MiniMax-M3.1-Flash', 'source':'comparison_recovery'}
            state['updated_at'] = datetime.now().isoformat()
            save(state_path, state)
    print(f'Recovered {doi}: {result.get("result")} trigger={result.get("trigger")}', flush=True)
    return result


def summarize(name, results):
    root = BASE / 'data/output' / name
    conn = pymssql.connect(**DB_CONFIG, login_timeout=10, timeout=60)
    try:
        cur = conn.cursor(as_dict=True)
        cur.execute(f'SELECT scholar,title,doi,source_review_result,comparison_status,m31_review_report_url FROM [{name}] ORDER BY scholar,doi')
        sources = cur.fetchall()
    finally:
        conn.close()
    by_doi = {x['doi'].casefold(): x for x in results}
    rows = []
    for source in sources:
        result = by_doi[source['doi'].casefold()]
        new = result.get('result') if substantive(result) else '复核未完成'
        rows.append({**source, 'source_model': 'MiniMax-M3', 'm31_model': 'MiniMax-M3.1-Flash',
                     'm31_result': new, 'changed': new in {'高风险', '低风险'} and new != source['source_review_result']})
    with (root / 'comparison.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {'table': name, 'papers': len(rows), 'completed': sum(x['comparison_status']=='completed' for x in rows),
               'valid_reviews': sum(substantive(x) for x in results),
               'source_high': sum(x['source_review_result']=='高风险' for x in rows),
               'm31_high': sum(x['m31_result']=='高风险' for x in rows),
               'm31_low': sum(x['m31_result']=='低风险' for x in rows),
               'errors': sum(not substantive(x) for x in results),
               'changed': sum(x['changed'] for x in rows)}
    save(root / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


async def batch(name):
    root = BASE / 'data/output' / name
    path = root / 'm31_review_results.json'
    results = [attach_saved_evidence(root, x) for x in load(path)]
    audit = root / 'audit_backups' / datetime.now().strftime('%Y%m%d_%H%M%S')
    audit.mkdir(parents=True)
    shutil.copy2(path, audit / path.name)
    failed = [value for value in results if not valid(value)]
    for value in failed:
        sp = Path(value['_evidence_path'].replace('_evidence.json', '_state.json'))
        shutil.copy2(sp, audit / sp.name)
    existing_job = asyncio.create_task(asyncio.to_thread(publish, name, [x for x in results if valid(x)], 'existing'))
    async def finish_one(value):
        try:
            result = await recover(name, value)
        except Exception as exc:
            result = {**value, '_recovery_error': f'{type(exc).__name__}: {exc}'}
        # No await inside the checkpoint replacement: parallel tasks cannot
        # overwrite each other's finished paper. Commit each as soon as ready.
        results[:] = [result if x['doi'] == result['doi'] else x for x in results]
        save(path, results)
        await asyncio.to_thread(publish, name, [result], 'recovery_' + doi_to_slug(result['doi']))
        return result
    await asyncio.gather(existing_job, *(finish_one(value) for value in failed))
    summarize(name, results)


async def main(names=NAMES):
    if any(name not in NAMES for name in names):
        raise ValueError('Invalid comparison namespace')
    await asyncio.gather(*(batch(name) for name in names))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(main(tuple(sys.argv[1:]) or NAMES))
