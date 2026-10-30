#!/usr/bin/env python3
"""Read-only preflight using the same gate as API recovery and PDF publication."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.services.review_validation import validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--doi', required=True)
    args = parser.parse_args()
    try:
        result = json.loads(Path(args.result).read_text())
        evidence = json.loads(Path(args.evidence).read_text())
        if not isinstance(result, dict) or not isinstance(evidence, dict) or not evidence:
            raise ValueError('Result and nonempty evidence must be JSON objects')
        if str(result.get('doi') or '').casefold() != args.doi.casefold():
            raise ValueError('Review DOI does not match the requested paper')
        _, gaps = validate(result, evidence)
    except Exception as exc:
        gaps = [f'{type(exc).__name__}: {exc}']
    print(json.dumps({'ok': not gaps, 'gaps': gaps}, ensure_ascii=False, indent=2))
    return 1 if gaps else 0


if __name__ == '__main__':
    raise SystemExit(main())
