"""Task-local resumable model calls; never turn CLI failures into verdicts."""
import json
from pathlib import Path
import re


def stable_prompt(prompt):
    # The shared builder allocates a fresh draft filename on every call; this
    # incidental filename must not hide an otherwise identical checkpoint.
    return re.sub(r'review_draft_[0-9a-f]{32}\.json', 'review_draft_CHECKPOINT.json', prompt)


def same_review_prompt(left, right):
    left, right = stable_prompt(left), stable_prompt(right)
    if left == right:
        return True
    fingerprint = r'_[0-9a-f]{12}(?=_evidence(?:_llm_summary)?\.json)'
    strip = lambda text: re.sub(fingerprint, '_CONTENT_CHECKED', text)
    if strip(left) != strip(right):
        return False
    paths = r'(/[^\s\"\'<>]+_[0-9a-f]{12}_evidence(?:_llm_summary)?\.json)'
    before, after = re.findall(paths, left), re.findall(paths, right)
    if not before or len(before) != len(after):
        return False
    for old, new in zip(before, after):
        try:
            a = json.dumps(json.loads(Path(old).read_text()), sort_keys=True, ensure_ascii=False)
            b = json.dumps(json.loads(Path(new).read_text()), sort_keys=True, ensure_ascii=False)
        except (OSError, ValueError):
            return False
        if strip(a) != strip(b):
            return False
    return True


def prior_session(prompt, base, checkpoints=None):
    """Match the full original prompt, including this paper's evidence paths."""
    root = Path.home() / '.claude/projects' / re.sub(r'[^a-zA-Z0-9]', '-', str(base))
    progress = {}
    if checkpoints:
        for response in checkpoints.glob('*_response.json'):
            try:
                value = json.loads(response.read_text())
                session = value.get('session_id')
                progress[session] = progress.get(session, 0) + int(value.get('response', {}).get('num_turns', 0))
            except (OSError, ValueError, TypeError):
                continue
    matches = []
    for path in sorted(root.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with path.open() as stream:
                for line in stream:
                    record = json.loads(line)
                    if record.get('type') == 'user':
                        content = record.get('message', {}).get('content')
                        if isinstance(content, str) and same_review_prompt(content, prompt):
                            matches.append((progress.get(path.stem, 0), path.stat().st_mtime, path.stem))
                        break
        except (OSError, ValueError):
            continue
    return max(matches)[2] if matches else None


def install(engine, job, save, load):
    from api.services.review_cli import run, prompt_key

    async def run_prompt(doi, review_label, prompt, max_turns, evidence_path='',
                         allowed_tools=('Bash', 'Read')):
        key = prompt_key(prompt)
        folder = job / 'cli_sessions'
        folder.mkdir(exist_ok=True)
        checkpoint = folder / f'{key}.json'
        state = load(checkpoint, {})
        if not state:
            previous = prior_session(prompt, engine.BASE_DIR, folder)
            if previous:
                state = {'session_id': previous}
        return await run(
            engine, doi, review_label, prompt, max_turns, allowed_tools,
            state=state, persist=lambda value: save(checkpoint, value),
            response_sink=lambda phase, value: save(folder / f'{key}_phase{phase}_response.json', value),
            progress=lambda value: save(job / 'progress.json', value),
        )

    async def judge(doi, report_json_path, input_dir, output_dir, result1, result2,
                    ev=None, coverage_gaps=None):
        prompt = engine._build_judge_prompt(doi, report_json_path, input_dir, result1, result2,
                                           ev=ev, coverage_gaps=coverage_gaps)
        result = await run_prompt(
            doi, 'Coverage repair' if coverage_gaps else 'Synthesis review', prompt,
            getattr(engine, 'COVERAGE_REPAIR_MAX_TURNS', 24) if coverage_gaps else engine.JUDGE_MAX_TURNS)
        return engine._clean_internal_references(result)

    engine._run_claude_prompt = run_prompt
    engine._run_judge = judge
