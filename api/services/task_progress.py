"""Read-only progress projection. Never resume jobs or publish review verdicts."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from api.models import TaskRecord

TERMINAL = {'completed', 'failed', 'superseded'}
SESSION_DIR = Path.home() / '.claude/projects/-home-sjtu-paper-integrity-checker'
_cache_lock = threading.Lock()
_session_cache = (0.0, [])


def _iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _json(path):
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _review_stages(paper):
    # Only inspect this paper's own checkpoints. Never match another task by DOI.
    if not paper.output_dir:
        return {}
    folder = Path(paper.output_dir) / 'review_artifacts'
    expected_report = str((Path(paper.output_dir) / 'report.json').resolve())
    expected_input = str(Path(paper.input_dir).resolve())
    candidates = []
    for path in folder.glob('*_state.json'):
        value = _json(path)
        identity = value.get('identity', {})
        if (identity.get('report', {}).get('path') != expected_report
                or identity.get('input', {}).get('path') != expected_input):
            continue
        try:
            if identity['report'].get('mtime_ns') != Path(expected_report).stat().st_mtime_ns:
                continue
        except OSError:
            continue
        candidates.append(value)
    newest = max(candidates, key=lambda d: d.get('updated_at', ''), default={})
    stages = newest.get('stages', {})
    return stages if isinstance(stages, dict) else {}


def _sessions():
    """Bound transcript reads and cache across dashboard/API polling requests."""
    global _session_cache
    with _cache_lock:
        if time.monotonic() - _session_cache[0] < 10:
            return _session_cache[1]
        recent = []
        for path in SESSION_DIR.glob('*.jsonl'):
            try:
                stamp = path.stat().st_mtime
                if time.time() - stamp < 24 * 3600:
                    recent.append((stamp, path))
            except OSError:
                continue
        sessions = []
        for stamp, path in sorted(recent, reverse=True)[:160]:
            try:
                with path.open('rb') as stream:
                    prefix = stream.read(262144).decode(errors='replace')
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - 262144))
                    tail = stream.read().decode(errors='replace')
            except OSError:
                continue
            # Bind identity to the initial user request, not later tool output.
            prompt, started = '', None
            for line in prefix.splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get('type') == 'user':
                    content = (record.get('message') or {}).get('content')
                    if isinstance(content, str):
                        prompt, started = content, record.get('timestamp')
                        break
                    if isinstance(content, list):
                        prompt = ' '.join(b.get('text', '') for b in content
                                          if isinstance(b, dict) and b.get('type') == 'text')
                        if prompt:
                            started = record.get('timestamp')
                            break
            activity, last_at = '', None
            for line in tail.splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                message = record.get('message') or {}
                blocks = message.get('content', []) if isinstance(message, dict) else []
                if not isinstance(blocks, list):
                    continue
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get('type') == 'tool_use':
                        name = block.get('name')
                        value = block.get('input', {})
                        text = str(value).lower()
                        if name == 'Read' and any(ext in text for ext in ('.png', '.jpg', '.jpeg')):
                            activity = '正在查看论文图像'
                        elif name == 'Read':
                            activity = '正在读取核查资料'
                        elif name == 'Bash' and any(term in text for term in ('openpyxl', '.xlsx', '.csv')):
                            activity = '正在核对源数据'
                        elif name == 'Bash' and any(term in text for term in ('fitz', '.pdf', 'pdftotext')):
                            activity = '正在核对论文正文或图注'
                        else:
                            activity = '正在执行核查工具'
                        last_at = record.get('timestamp')
                    elif block.get('type') == 'tool_result':
                        activity = ('一次工具调用返回错误，等待后续处理' if block.get('is_error')
                                    else '核查工具已返回，继续分析')
                        last_at = record.get('timestamp')
            if prompt:
                sessions.append({'prompt': prompt, 'started_at': started,
                                 'last_activity_at': last_at, 'activity': activity})
        _session_cache = (time.monotonic(), sessions)
        return sessions


def _activity(paper, task, sessions, now):
    matches = [s for s in sessions if paper.input_dir and
               (paper.input_dir + '\n' in s['prompt'] or paper.input_dir + '/' in s['prompt']
                or paper.input_dir + ' ' in s['prompt'] or s['prompt'].endswith(paper.input_dir))
               and _iso(s['started_at']) and _iso(s['started_at']) >= task.created_at.astimezone(timezone.utc)]
    session = max(matches, key=lambda s: s['started_at'], default={})
    stamp = _iso(session.get('last_activity_at'))
    age = max(0, int((now - stamp).total_seconds())) if stamp else None
    state = ('terminal' if task.status.value in TERMINAL else
             'unknown' if age is None else 'recent_activity' if age <= 180 else 'quiet')
    return {'last_activity_at': stamp.isoformat() if stamp else None,
            'seconds_since_activity': age, 'activity_state': state,
            'activity_text': session.get('activity', '') or '暂无可读取的工具活动',
            'round_started_at': session.get('started_at')}


def build_task_progress(task: TaskRecord, *, sessions=None, now=None):
    now = now or datetime.now(timezone.utc)
    sessions = _sessions() if sessions is None and task.status.value not in TERMINAL else (sessions or [])
    phase = task.status.value
    cancelled = task.stage == 'cancelled_by_user'
    active_phase = task.stage if phase == 'failed' else phase
    stage_index = {'queued': 0, 'extracting': 0, 'detecting': 1, 'reviewing': 2,
                   'generating_report': 3, 'done': 3}.get(active_phase, 0)
    records = (task.result or {}).get('detection_reports', [])
    skipped_review = bool(records) and all(r.get('chushen_result') == '低风险'
                                           and not r.get('review_result') for r in records)
    steps = []
    for i, (key, label) in enumerate([('prepare', '文件准备'), ('detect', '初审'),
                                     ('review', '复审'), ('publish', '报告与结果确认')]):
        state = ('completed' if phase == 'completed' or i < stage_index else
                 'active' if i == stage_index else 'pending')
        if phase == 'completed' and skipped_review and key == 'review':
            state = 'skipped'
        if phase in {'failed', 'superseded'} and (i == stage_index or cancelled):
            state = 'stopped'
        steps.append({'key': key, 'label': label, 'state': state})
    details = []
    for paper in task.papers:
        stages = _review_stages(paper) if phase == 'reviewing' else {}
        final = stages.get('final') or {}
        final_ready = (final.get('result') in {'高风险', '低风险'}
                       and final.get('trigger') != 'review_error'
                       and not final.get('_coverage_gaps') and not final.get('_coverage_error'))
        round_name = ('复审结果已保存，等待报告处理' if final_ready else
                      '综合复核后的补充核查' if stages.get('judge') else
                      '综合复核' if stages.get('reviewer2') else
                      '第二轮复审' if stages.get('reviewer1') else '第一轮复审')
        item = {'doi': paper.doi, 'fold_name': paper.fold_name,
                'submission_no': paper.submission_no,
                'review_stage': round_name if phase == 'reviewing' else None,
                'saved_review_rounds': sum(bool(stages.get(k)) for k in ('reviewer1', 'reviewer2'))}
        item.update(_activity(paper, task, sessions, now))
        details.append(item)
    label = {'queued': '等待执行', 'extracting': '正在准备文件', 'detecting': '初审中',
             'reviewing': '复审中', 'generating_report': '生成报告并确认结果',
             'completed': '检测完成', 'failed': '任务失败', 'superseded': '已被新任务替代'}[phase]
    if phase == 'reviewing' and len(details) == 1:
        label = details[0]['review_stage']
    if cancelled:
        label = '已人工终止'
    end = task.completed_at.astimezone(timezone.utc) if task.completed_at else now
    return {'task_id': task.task_id, 'status': phase, 'label': label,
            'progress_type': 'milestones', 'percent': 100 if phase == 'completed' else None,
            'indeterminate': phase not in TERMINAL, 'steps': steps, 'papers': details,
            'elapsed_seconds': max(0, int((end - task.created_at.astimezone(timezone.utc)).total_seconds())),
            'observed_at': now.isoformat(), 'error': task.error,
            'note': '显示已完成阶段；复审轮次和核查次数不等于总耗时百分比。'}
