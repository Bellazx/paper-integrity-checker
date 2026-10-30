"""Read-only regression of the new production content gate on 14 historical misses.

The detector receives only the manuscript. Archived review findings are never
passed to it. This isolates the new gate; it is not a full pipeline rerun.
"""
from concurrent.futures import ProcessPoolExecutor, as_completed
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
from threading import Lock

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
ROUND = BASE / 'data/yujing_scholar_zhuoyue_20261026_round_1'


def read(path):
    return json.loads(Path(path).read_text())


def save(path, obj):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    temporary.replace(path)


def worker(paper, folder, reuse=None):
    from modules import initial_content_screen as screen
    from modules.chinese_report_generator import _compute_overall_risk
    from utils.llm_client import get_client
    dest = Path(folder) / paper['slug']
    dest.mkdir()
    started = time.time()
    result = {'doi': paper['doi'], 'source_pdf': paper['pdf'], 'status': 'running'}
    save(dest / 'result.json', result)
    call = 0
    call_lock = Lock()
    cached = {}
    if reuse:
        previous = Path(reuse) / paper['slug']
        for f in previous.glob('call_*.json'):
            response_file = previous / f.name.replace('call_', 'response_')
            if response_file.exists():
                cached[read(f)['prompt_sha256']] = (read(response_file)['response'], str(response_file))
    # Observe transport completion without changing production request settings.
    client = get_client()
    create = client.chat.completions.create
    transport_call = 0

    def traced_create(*args, **kwargs):
        nonlocal transport_call
        with call_lock:
            transport_call += 1
            number = transport_call
        response = create(*args, **kwargs)
        choice = response.choices[0]
        save(dest / f'transport_{number}.json', {
            'finish_reason': choice.finish_reason,
            'usage': response.usage.model_dump() if response.usage else None,
            'message': choice.message.model_dump()})
        return response

    client.chat.completions.create = traced_create

    def request(prompt):
        nonlocal call
        with call_lock:
            call += 1
            current = call
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        save(dest / f'call_{current}.json', {
            'status': 'running', 'started': datetime.now().isoformat(),
            'prompt_sha256': digest,
            'prompt_length': len(prompt),
            'reused_from': cached.get(digest, (None, None))[1],
            'stage': 'verify' if '\n候选:\n' in prompt else 'discover'})
        response = cached[digest][0] if digest in cached else screen._request(prompt)
        save(dest / f'response_{current}.json', {'response': response})
        return response

    request.parallel_safe = True

    try:
        issues, warnings, audit = screen.screen_manuscript(paper['pdf'], request=request)
        save(dest / 'audit.json', audit)
        save(dest / 'issues.json', issues)
        assessment = _compute_overall_risk({
            'paper': {}, 'image_duplicates': [], 'data_anomalies': issues,
            'reference_issues': [], 'image_splicing': []})
        if isinstance(assessment.get('high_dimensions'), set):
            assessment['high_dimensions'] = sorted(assessment['high_dimensions'])
        result.update(status='complete', issue_count=len(issues),
                      gate_risk=assessment['level'], warnings=warnings,
                      assessment=assessment)
    except Exception as exc:
        result.update(status='incomplete', error=str(exc),
                      error_type=type(exc).__name__, traceback=traceback.format_exc())
    result.update(elapsed_seconds=round(time.time()-started, 1), model_calls=call)
    save(dest / 'result.json', result)
    return result


def main():
    from config import MINIMAX_MODEL
    import os
    parser = argparse.ArgumentParser()
    parser.add_argument('doi', nargs='?')
    parser.add_argument('--reuse', type=Path, help='Reuse only exact prompt matches from an observed run')
    parser.add_argument('--summarize', type=Path, help='Rebuild a terminal run summary from per-paper receipts')
    args = parser.parse_args()
    if args.summarize:
        manifest = read(args.summarize/'manifest.json')
        results = []
        for paper in manifest['papers']:
            result = read(args.summarize/paper['slug']/'result.json')
            assert result['doi'] == paper['doi'] and result['source_pdf'] == paper['pdf']
            assert result['status'] in ('complete', 'incomplete')
            results.append(result)
        save(args.summarize/'summary.json', results)
        print(f'Summarized {len(results)} terminal paper receipts', flush=True)
        return
    if args.reuse:
        assert read(args.reuse/'manifest.json')['model'] == os.getenv('INITIAL_CONTENT_MODEL', MINIMAX_MODEL)
    before = {p['doi'].lower(): p for p in read(ROUND/'database_before.json')}
    papers = {p['doi'].lower(): p for p in read(ROUND/'papers.json')}
    targets = []
    for review in read(ROUND/'reassessment_current/results_current.json'):
        doi = review['doi'].lower()
        if review['result'] != '高风险' or before[doi]['risk_level'] != '低风险':
            continue
        paper = dict(papers[doi])
        paper['pdf'] = read(paper['report_json'])['paper']['filepath']
        assert Path(paper['pdf']).is_file()
        targets.append(paper)
    assert len(targets) == 14 and len({p['doi'].lower() for p in targets}) == 14
    if args.doi:
        targets = [p for p in targets if p['doi'].lower() == args.doi.lower()]
        assert len(targets) == 1, 'Diagnostic DOI must be one of the 14 frozen targets'
    dest = ROUND / ('initial_gate_validation_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    dest.mkdir()
    save(dest/'manifest.json', {'mode': 'production content gate + production risk scorer',
        'database_write': False, 'review_hints_supplied': False,
        'model': os.getenv('INITIAL_CONTENT_MODEL', MINIMAX_MODEL), 'papers': targets,
        'reuse_exact_prompts_from': str(args.reuse) if args.reuse else None,
        'code_sha256': hashlib.sha256((BASE/'modules/initial_content_screen.py').read_bytes()).hexdigest()})
    print(f'OUTPUT={dest}', flush=True)
    results = []
    with ProcessPoolExecutor(max_workers=14) as pool:
        futures = {pool.submit(worker, p, str(dest), args.reuse): p for p in targets}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            save(dest/'summary.json', results)
            print(json.dumps({k: result.get(k) for k in
                  ('doi', 'status', 'gate_risk', 'issue_count', 'error', 'elapsed_seconds')},
                  ensure_ascii=False, default=str), flush=True)
    print('ALL_DONE', flush=True)


if __name__ == '__main__':
    main()
