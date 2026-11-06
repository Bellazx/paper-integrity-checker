"""Real CLI context handoff on two synthetic ledger items; no business reports."""
import asyncio
import json
from pathlib import Path
import time

from review_v3 import ledger, session


async def main():
    root = Path.cwd() / 'experiments/calibration-handoff-smoke'
    root.mkdir(parents=True, exist_ok=False)
    (root / 'dossier').mkdir()
    (root / 'dossier/index.json').write_text(json.dumps({'paper': {'title': 'Synthetic handoff smoke'}, 'docs': [], 'data_files': []}))
    first = {'status': 'issue', 'severity': 'minor', 'location': 'Table 1 p1', 'issue_type': '孤立单格错误',
             'finding': '一个派生格写成 10，正确值为 11', 'basis': '这是合成测试，其他数值和比较均不受影响',
             'why': '孤立单格不改变比较结论'}
    meta = {'require_category': True, 'allow_new': False}
    first_path = root / 'ledgers/cal_all.json'
    rest_path = root / 'ledgers/cal_rest.json'
    ledger.create(first_path, meta, [{'id': 'new:first', 'dimension': 'data', 'first': first}])
    instructions = ('这是合成工具冒烟测试，不涉及真实论文。只使用 Bash 调用 pv3 item 和 pv3 ledger update，'
                    '不要读其他文件或调查。把指定条目用 category=M1、status=issue、why=孤立单格不改变比较结论 写入，'
                    '此次更新故意省略 dimension，以验证已有字段继承。确认成功后回复完成。指定条目：')
    spec = session.SessionSpec('cal_all', root, instructions + 'new:first', 'calibrator',
                               ledger_path=first_path, max_chunks=2, deadline=time.monotonic() + 180,
                               continue_prompt=lambda s: instructions + 'new:first')
    start = time.monotonic()
    outcome = await session.run(spec)
    assert outcome.status == 'done', outcome
    saved = json.loads((root / 'sessions/cal_all.json').read_text())
    first_bytes = first_path.read_bytes()
    ledger.create(rest_path, meta, [{'id': 'new:second', 'dimension': 'data', 'first': first}])
    carry = session.continue_as('cal_all', 'cal_rest', root)
    assert carry['continuation_mode'] == 'resume'
    followup = instructions + 'new:second。环境已切换到补评级台账，new:first 已保存，不要重写。'
    spec = session.SessionSpec('cal_rest', root, followup, 'calibrator', ledger_path=rest_path,
                               max_chunks=carry['continuation_base_chunks'] + 2, deadline=time.monotonic() + 180,
                               continue_prompt=lambda s: followup, restart_prompt=lambda s: followup)
    outcome = await session.run(spec)
    restored = json.loads((root / 'sessions/cal_rest.json').read_text())
    assert outcome.status == 'done', outcome
    assert saved['session_id'] == restored['session_id']
    assert first_path.read_bytes() == first_bytes
    assert ledger.load(rest_path)['items']['new:second']['category'] == 'M1'
    assert restored['metrics'][0]['resumed']
    result = {'status': 'passed', 'wall_seconds': time.monotonic() - start,
              'same_session_id': True, 'original_ledger_unchanged': True,
              'first_metrics': saved['metrics'], 'recovery_metrics': restored['metrics']}
    (root / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
