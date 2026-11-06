"""Isolated latency comparison; never publishes reports or updates a database.

--live-reports makes real translation calls using an existing final review.
Workbook timings compare identical complete sheet reads, not full reviews.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def workbook_benchmark(path, out):
    import openpyxl
    from review_v3.workbook_cache import read_sheet, _encode
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = max(book.worksheets, key=lambda s: (s.max_row or 0) * (s.max_column or 0)).title
    book.close()

    def measure(cache):
        started = time.monotonic()
        digest = hashlib.sha256()
        count = 0
        for row in read_sheet(path, sheet, cache)[1]:
            digest.update(json.dumps(row, default=_encode).encode())
            count += 1
        return {'seconds': round(time.monotonic() - started, 4), 'rows': count, 'sha256': digest.hexdigest()}

    raw = [measure(None) for _ in range(2)]
    cache = out / 'workbook_cache'
    cold = measure(cache)
    warm = [measure(cache) for _ in range(2)]
    assert len({r['sha256'] for r in [*raw, cold, *warm]}) == 1
    return {'file': str(path), 'sheet': sheet, 'uncached': raw, 'cache_build': cold, 'cached': warm}


async def report_benchmark(review, out):
    from api.models import PaperInfo
    from api.services import detection_reports_worker as worker
    from api.services.api_capacity import WorkLimiter
    from api.services.report import generate_english_report
    from api.config import PYTHON
    import fitz

    async def chinese(results, task_dir, **kwargs):
        target = out / kwargs['report_namespace']
        target.mkdir(parents=True, exist_ok=True)
        source = target / 'final.json'
        source.write_text(json.dumps(results[0], ensure_ascii=False))
        proc = await asyncio.create_subprocess_exec(
            str(PYTHON), str(ROOT / 'scripts/review_v3_preview_pdf.py'), '--out', str(target), str(source),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode:
            raise RuntimeError(stderr.decode())
        paths = list(target.glob('review_*.pdf'))
        if len(paths) != 1:
            raise RuntimeError(f'Chinese preview failed: {stdout.decode()} {stderr.decode()}')
        return [str(paths[0])]

    # Only the publication side effects are replaced; the parallel coordinator
    # and isolated English subprocess are the production implementation.
    worker.generate_reports = chinese
    worker.REVIEW_DIR = out
    worker.api_work = WorkLimiter(2)
    paper = PaperInfo(doi=review['doi'], doi_slug=review.get('_report_slug') or 'paper', language='both')
    results = {}
    for mode in ('sequential', 'parallel'):
        value = {**review, '_report_namespace': mode}
        deadline = time.monotonic() + 5 * 60
        started = time.monotonic()
        if mode == 'sequential':
            zh = await chinese([value], out / 'tasks' / mode, report_namespace=mode)
            en = await generate_english_report(value, out / mode, out / 'tasks' / mode, kind='review',
                                               slug=worker._review_report_slug(paper, value), deadline=deadline)
            rendered = {'zh': zh, 'en': en}
        else:
            rendered = await worker._render_review_pair(paper, value, out / 'tasks' / mode, deadline)
        elapsed = time.monotonic() - started
        pdfs = {}
        for language, paths in rendered.items():
            if isinstance(paths, BaseException):
                raise paths
            path = paths[0] if isinstance(paths, list) else paths
            with fitz.open(path) as pdf:
                text = '\n'.join(page.get_text() for page in pdf)
                pdfs[language] = {'path': path, 'pages': len(pdf), 'text_chars': len(text)}
                Path(path).with_suffix('.txt').write_text(text)
        results[mode] = {'seconds': round(elapsed, 3), 'pdfs': pdfs}
        print(json.dumps({mode: results[mode]}, ensure_ascii=False), flush=True)
        (out / 'report_timings.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    return results


def initial_benchmark(findings, out):
    from modules.initial_report_bundle import generate_initial_pdfs
    from modules.chinese_report_generator import generate_chinese_pdf
    from modules.english_report_generator import generate_english_initial_pdf
    result = {}
    for mode in ('sequential', 'parallel'):
        target = out / mode
        target.mkdir(parents=True, exist_ok=True)
        value = copy.deepcopy(findings)
        os.environ['PAPER_LLM_DEADLINE'] = str(time.monotonic() + 285)
        started = time.monotonic()
        if mode == 'sequential':
            zh, _ = generate_chinese_pdf(value, str(target / 'zh'))
            if not zh:
                raise RuntimeError('Chinese initial benchmark failed')
            en = generate_english_initial_pdf(value, target / 'en')
        else:
            os.environ['PAPER_INITIAL_REPORT_LANGUAGE'] = 'both'
            os.environ['PAPER_ENGLISH_REPORT_DIR'] = str(target / 'en')
            zh, _ = generate_initial_pdfs(value, str(target / 'zh'), chinese_renderer=generate_chinese_pdf)
            en = value['_english_initial_pdf']
        result[mode] = {'seconds': round(time.monotonic() - started, 3), 'zh': zh, 'en': en}
        print(json.dumps({'initial': {mode: result[mode]}}, ensure_ascii=False), flush=True)
        (out / 'timings.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workbook', type=Path)
    parser.add_argument('--review', type=Path)
    parser.add_argument('--live-reports', action='store_true')
    parser.add_argument('--live-initial', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    result = {}
    if args.workbook:
        result['workbook'] = workbook_benchmark(args.workbook.resolve(), args.out)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        (args.out / 'timings.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    if args.live_reports:
        value = json.loads(args.review.read_text())
        review = value[0] if isinstance(value, list) else value
        result['reports'] = asyncio.run(report_benchmark(review, args.out / 'reports'))
    if args.live_initial:
        result['initial'] = initial_benchmark(json.loads(args.live_initial.read_text()), args.out / 'initial')
    (args.out / 'timings.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
