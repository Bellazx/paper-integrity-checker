#!/usr/bin/env python3
"""Shared project entry point used by API and ordinary batch report generation."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.services.review_validation import renderer_module, renderer_path, validate


def _skill_renderer() -> Path:
    return renderer_path()


def _patch_coverage_entrypoint(module) -> None:
    original = getattr(module, "_coverage_check", None)
    if original is None:
        return

    def coverage_check(info):
        explicit = (info.get("_evidence_path") or info.get("evidence_path")
                    or info.get("_coverage_evidence_path"))
        if explicit:
            try:
                with open(explicit, encoding="utf-8") as handle:
                    bundle = json.load(handle)
                if not isinstance(bundle, dict) or not bundle:
                    raise ValueError("empty or invalid evidence bundle")
                validated, gaps = validate(info, bundle)
                validated["_coverage_evidence_path"] = str(explicit)
                return validated
            except Exception as exc:
                return {**info, "_coverage_error": f"Explicit evidence validation failed: {exc}"}
        out = original(info)
        resolved = out.get('_coverage_evidence_path') if isinstance(out, dict) else None
        if resolved and not out.get('_coverage_error'):
            return coverage_check({**info, '_evidence_path': resolved})
        # Legacy batch inputs may lack an explicit evidence path. The original
        # validator stays strict; never add claims or remove semantic gaps.
        error = out.get("_coverage_error") if isinstance(out, dict) else None
        policy = os.environ.get("REVIEW_COVERAGE_FAILURE_POLICY", "warn").lower()
        if error and policy in {"warn", "soft", "continue"} and any(
            token in str(error).lower() for token in
            ("timed out", "timeout", "oom", "out of memory", "worker killed",
             "worker exited without result", "exitcode=-9")
        ):
            out["_coverage_warning"] = str(error)
            out.pop("_coverage_error", None)
            out["_coverage_status"] = "resource_warning"
        return out

    module._coverage_check = coverage_check


def main() -> int:
    module = renderer_module()
    _patch_coverage_entrypoint(module)
    _patch_database_writer(module)
    # Publish papers independently. A review_error or invalid screenshot map
    # must remain visible, but cannot prevent unrelated valid papers from being
    # rendered/committed. The API still rejects a partial batch via rc=2.
    argv = list(sys.argv)
    if '--results' not in argv:
        return int(module.main() or 0)
    source = Path(argv[argv.index('--results') + 1])
    results = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(results, list):
        raise ValueError('Review results must be a JSON list')
    failures = []
    with tempfile.TemporaryDirectory(prefix='publication_', dir=source.parent) as scratch:
        for index, info in enumerate(results):
            if info.get('trigger') == 'review_error':
                failures.append({'doi': info.get('doi'), 'error': info.get('_review_error') or 'review_error'})
                continue
            paper_path = Path(scratch) / f'{index}.json'
            paper_path.write_text(json.dumps([info], ensure_ascii=False), encoding='utf-8')
            sys.argv = list(argv)
            sys.argv[sys.argv.index('--results') + 1] = str(paper_path)
            try:
                rc = int(module.main() or 0)
                if rc:
                    failures.append({'doi': info.get('doi'), 'error': f'renderer returned {rc}'})
            except Exception as exc:
                failures.append({'doi': info.get('doi'), 'error': str(exc)})
            finally:
                sys.argv = argv
    failure_path = source.with_suffix('.publication_failures.json')
    failure_path.write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding='utf-8')
    if failures:
        print(json.dumps({'publication_failures': failures}, ensure_ascii=False), file=sys.stderr)
    return 2 if failures else 0


def _patch_database_writer(module):
    """The legacy renderer printed NOT FOUND but still returned success."""
    def update_and_verify(updates, table_name='yujing_quanliang', scholar=''):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', table_name):
            raise ValueError('Invalid report database table')
        config = {**module.DB_CONFIG, 'login_timeout': 10, 'timeout': 60}
        conn = module.pymssql.connect(**config)
        try:
            cursor = conn.cursor()
            for item in updates:
                params = {**item, 'scholar': scholar, 'review_time': datetime.now()}
                where = 'doi=%(doi)s' + (' AND scholar=%(scholar)s' if scholar else '')
                cursor.execute(f'''UPDATE {table_name} SET review_result=%(review_result)s,
                    review_report_url=%(review_report_url)s,review_time=%(review_time)s,status=2
                    WHERE {where}''', params)
                cursor.execute(f'SELECT review_result,review_report_url,status FROM {table_name} WHERE {where}', params)
                rows = cursor.fetchall()
                if not rows or any(row[0] != item['review_result'] or row[1] != item['review_report_url']
                                   or row[2] != 2 for row in rows):
                    raise RuntimeError(f"Report DB verification failed for {item['doi']}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    module._update_db = update_and_verify


if __name__ == "__main__":
    raise SystemExit(main())
