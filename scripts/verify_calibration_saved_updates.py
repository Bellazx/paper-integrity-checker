"""Replay saved tool JSON through old/new validation; no model or shell execution."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile

from review_v3 import ledger


def main():
    source = Path('/home/library/paper-review-parallel-dev-20261106/experiments/calibration-analysis-20261106/metrics.json')
    old_path = Path('/home/library/paper-review-parallel-dev-20261106/experiments/api-five-parallel-replay-20261106/snapshot/review_v3/ledger.py')
    spec = importlib.util.spec_from_file_location('baseline_ledger', old_path)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    results = []
    with tempfile.TemporaryDirectory(prefix='saved-calibration-updates-') as tmp:
        for paper, versions in json.loads(source.read_text()).items():
            for saved in versions['current']:
                if not saved['writes']:
                    continue
                commands = []
                for line in Path(saved['transcript']).read_text().splitlines():
                    event = json.loads(line)
                    if not event.get('timestamp') or event.get('type') != 'assistant':
                        continue
                    for block in (event.get('message') or {}).get('content') or []:
                        cmd = (block.get('input') or {}).get('command', '')
                        if block.get('type') == 'tool_use' and 'pv3 ledger update' in cmd:
                            commands.append(cmd)
                # Parse JSON only. Never execute saved shell commands.
                body = commands[0].split("<<'EOF'", 1)[1].lstrip()
                entries, _ = json.JSONDecoder().raw_decode(body)
                state_path = Path(saved['session_file'])
                recorded = json.loads((state_path.parent.parent / 'ledgers' / state_path.name).read_text())
                seeds = [{k: r[k] for k in ('id', 'title', 'dimension', 'first') if k in r}
                         for r in recorded['items'].values()]
                outputs = {}
                for label, module in [('before', old), ('after', ledger)]:
                    path = Path(tmp) / paper / label / 'ledgers/cal.json'
                    module.create(path, {'allow_new': False, 'require_category': True}, copy.deepcopy(seeds))
                    applied, errors = module.update(path, copy.deepcopy(entries), 'calibrator')
                    outputs[label] = {'applied': applied, 'dimension_rejections': sum('新发现必须给出 dimension' in e for e in errors),
                                      'other_errors': [e for e in errors if '新发现必须给出 dimension' not in e]}
                    if label == 'after':
                        for entry in entries:
                            if entry['id'] not in recorded['items']:
                                continue
                            actual = module.load(path)['items'][entry['id']]
                            assert actual['status'] == entry['status'], entry['id']
                            if entry['status'] == 'issue':
                                assert actual['category'] == entry['category'], entry['id']
                            assert actual['first'] == recorded['items'][entry['id']]['first']
                assert outputs['after']['dimension_rejections'] == 0
                assert outputs['after']['applied'] - outputs['before']['applied'] == outputs['before']['dimension_rejections']
                assert outputs['before']['other_errors'] == outputs['after']['other_errors']
                results.append({'paper': paper, **outputs})
    assert sum(r['before']['dimension_rejections'] for r in results) == 70
    Path('saved-calibration-update-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
