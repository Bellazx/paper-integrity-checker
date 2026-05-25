#!/usr/bin/env python3
"""Check decimal precision patterns in source data files for fraud indicators.

Usage:
    python3 check_decimal_precision.py <paper_input_dir>

Outputs a summary of decimal precision per sheet per file, highlighting:
  - Cross-sheet precision mismatches (e.g., 2 vs 9 decimals for same measurement type)
  - Suspiciously uniform precision (all values at exactly N decimals)
  - Round number dominance
"""
import sys
from pathlib import Path
import pandas as pd
import numpy as np


def get_decimal_places(val):
    s = f"{val:.15g}"
    if '.' not in s:
        return 0
    return len(s.rstrip('0').split('.')[1])


def analyze_file(filepath: Path):
    results = []
    try:
        if filepath.suffix.lower() in ('.xlsx', '.xls'):
            engine = 'xlrd' if filepath.suffix.lower() == '.xls' else 'openpyxl'
            raw = pd.read_excel(filepath, sheet_name=None, engine=engine, header=None)
        else:
            return results

        for sheet_name, df in raw.items():
            if len(df) < 2:
                continue
            header_row = df.iloc[0].tolist()
            data = df.iloc[1:].copy()
            cols_info = []

            for j, col in enumerate(data.columns):
                vals = pd.to_numeric(data[col], errors='coerce').dropna()
                if len(vals) < 3:
                    continue
                header = str(header_row[j]) if j < len(header_row) and pd.notna(header_row[j]) else f"col_{j}"

                decimals = vals.apply(get_decimal_places)
                min_dec = int(decimals.min())
                max_dec = int(decimals.max())
                mean_dec = float(decimals.mean())
                uniform = min_dec == max_dec
                n_round = int(sum(1 for v in vals if float(v) == round(float(v), 0)))

                cols_info.append({
                    "header": header,
                    "n": len(vals),
                    "min_dec": min_dec,
                    "max_dec": max_dec,
                    "mean_dec": round(mean_dec, 1),
                    "uniform": uniform,
                    "pct_round": round(100 * n_round / len(vals), 1) if len(vals) > 0 else 0,
                })

            if cols_info:
                results.append({
                    "file": filepath.name,
                    "sheet": sheet_name,
                    "columns": cols_info,
                })
    except Exception as e:
        print(f"ERROR loading {filepath.name}: {e}", file=sys.stderr)

    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 check_decimal_precision.py <paper_input_dir>")
        sys.exit(1)

    paper_dir = Path(sys.argv[1])
    data_files = []
    for ext in ('*.xlsx', '*.xls'):
        data_files.extend(paper_dir.rglob(ext))

    if not data_files:
        print("No xlsx/xls files found.")
        return

    all_results = []
    for f in sorted(data_files):
        all_results.extend(analyze_file(f))

    flags = []
    all_precisions = {}

    for sheet_info in all_results:
        fname = sheet_info["file"]
        sname = sheet_info["sheet"]
        print(f"\n=== {fname} / {sname} ===")
        for c in sheet_info["columns"]:
            marker = ""
            if c["uniform"] and c["min_dec"] >= 5:
                marker = " *** SUSPICIOUS: uniform high precision"
                flags.append(f"{fname}/{sname}/{c['header']}: all values at exactly {c['min_dec']} decimals")
            if c["pct_round"] > 80 and c["n"] > 10:
                marker += " ** HIGH ROUND%"
            print(f"  {c['header']:30s} n={c['n']:4d}  dec=[{c['min_dec']}-{c['max_dec']}] mean={c['mean_dec']:.1f}  round%={c['pct_round']:.0f}%{marker}")

            key = c["header"].lower().strip()
            if key not in all_precisions:
                all_precisions[key] = []
            all_precisions[key].append({
                "file": fname, "sheet": sname,
                "min_dec": c["min_dec"], "max_dec": c["max_dec"],
                "mean_dec": c["mean_dec"],
            })

    print("\n" + "=" * 60)
    print("CROSS-SHEET PRECISION COMPARISON:")
    for col_name, entries in all_precisions.items():
        if len(entries) < 2:
            continue
        means = [e["mean_dec"] for e in entries]
        if max(means) - min(means) >= 3:
            print(f"\n  *** MISMATCH: '{col_name}' has precision range [{min(means):.1f} - {max(means):.1f}]:")
            for e in entries:
                print(f"      {e['file']}/{e['sheet']}: dec=[{e['min_dec']}-{e['max_dec']}] mean={e['mean_dec']:.1f}")
            flags.append(f"Cross-sheet precision mismatch for '{col_name}': {min(means):.1f} vs {max(means):.1f} decimals")

    if flags:
        print(f"\n{'=' * 60}")
        print(f"FLAGS ({len(flags)}):")
        for f in flags:
            print(f"  - {f}")
    else:
        print("\nNo suspicious patterns found.")


if __name__ == "__main__":
    main()
