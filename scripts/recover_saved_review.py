#!/usr/bin/env python3
"""Recover a lost repair checkpoint from its original local reviewer transcript.

Default: validate only. --apply stores the original result, with provenance;
the normal pipeline must still validate, render, and persist the final report.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.config import TASKS_DIR
from api.models import TaskRecord, TaskStatus
from api.services.review import _extract_json_from_text, _review_state_path, _store_stage
from api.services.review_validation import validate


def recover(task_id, session_id, apply=False, save_incomplete=False):
    if not re.fullmatch(r'\d{8}-[a-f0-9]{8}', task_id) or not re.fullmatch(r'[a-f0-9-]{36}', session_id):
        raise ValueError('Invalid task/session identifier')
    task = TaskRecord.model_validate_json((TASKS_DIR / task_id / 'status.json').read_text())
    if task.status != TaskStatus.FAILED or task.stage == 'cancelled_by_user' or len(task.papers) != 1:
        raise ValueError('Only an explicitly failed single-paper task may be recovered')
    paper = task.papers[0]
    path = Path.home() / '.claude/projects/-home-sjtu-paper-integrity-checker' / (session_id + '.jsonl')
    raw = path.read_bytes()
    records = []
    for line in raw.splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    first_user = next(r for r in records if r.get('type') == 'user')
    prompt = first_user['message']['content']
    if isinstance(prompt, list):
        prompt = '\n'.join(b.get('text', '') for b in prompt if isinstance(b, dict))
    if (paper.input_dir not in prompt or task_id not in prompt
            or '本次必须补齐的覆盖缺口' not in prompt):
        raise ValueError('Transcript is not this task\'s coverage-repair request')
    results = []
    for record in records:
        if record.get('type') != 'assistant':
            continue
        for block in (record.get('message') or {}).get('content', []):
            if not isinstance(block, dict) or block.get('type') != 'text':
                continue
            value = _extract_json_from_text(block.get('text', ''))
            if isinstance(value, dict) and value.get('doi', '').casefold() == paper.doi.casefold():
                if value.get('result') in {'高风险', '低风险'} and value.get('trigger') != 'review_error':
                    results.append((record.get('timestamp'), value))
    if not results:
        raise ValueError('No substantive review JSON found')
    stamp, result = results[-1]
    state_path = _review_state_path(paper.doi, paper.report_json, paper.input_dir,
                                    Path(paper.output_dir) / 'review_artifacts')
    state = json.loads(state_path.read_text())
    evidence = json.loads(Path(state['stages']['evidence_path']).read_text())
    _, gaps = validate(result, evidence)
    print(json.dumps({'task_id': task_id, 'session': session_id, 'timestamp': stamp,
                      'coverage_gaps': gaps, 'apply': apply}, ensure_ascii=False), flush=True)
    if gaps and not save_incomplete:
        raise ValueError('Recovered result has unresolved coverage gaps')
    if apply:
        _store_stage(state_path, state, 'coverage_repair', result)
        _store_stage(state_path, state, 'checkpoint_recovery', {
            'session_id': session_id, 'timestamp': stamp,
            'transcript_sha256': hashlib.sha256(raw).hexdigest(),
            'unresolved_gaps_at_recovery': gaps,
            'note': 'Original reviewer JSON restored without editing its evidence or verdict',
        })


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('task_id')
    parser.add_argument('session_id')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--save-incomplete', action='store_true',
                        help='Preserve an incomplete repair for further review; never publish it')
    args = parser.parse_args()
    recover(args.task_id, args.session_id, args.apply, args.save_incomplete)
