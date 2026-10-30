#!/usr/bin/env python3
"""Run the isolated 2026-09 review round for the August/September journal sets."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pymssql


BASE = Path("/home/sjtu/paper-integrity-checker")
SOURCE = BASE / "scripts" / "run_scholar_youjiabin_round.py"
INPUT_ROOTS = (
    BASE / "data" / "input" / "8月八大刊新增",
    BASE / "data" / "input" / "9月八大刊新增",
)
INIT_ROOT = BASE / "data" / "output" / "yujing_4050_202607" / "init"
EVIDENCE_ROOT = BASE / "data" / "output" / "review_v2"

DB = dict(
    server="10.119.5.44",
    user="yujing",
    password="fengxian_YJ514",
    database="lunwenyujing",
    charset="utf8",
    timeout=30,
)


def load_runner():
    spec = importlib.util.spec_from_file_location("aug_sep_review_shared", SOURCE)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载批次编排模块: {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__file__ = str(Path(__file__).resolve())

    module.ROUND_NAME = "yujing_4050_aug_sep_202609_round_1"
    module.ROUND_DIR = BASE / "data" / module.ROUND_NAME
    module.CONFIG_DIR = module.ROUND_DIR / "config"
    module.SCHOLAR = "8月八大刊新增、9月八大刊新增"
    module.PROMPT_TAG = "yujing_4050_aug_sep_202609_round1"
    module.INIT_ROOT = INIT_ROOT
    module.CHINESE_REPORT_DIR = BASE / "data" / "output" / "yujing_4050_202607" / "chinese_reports"
    module.START_BATCH = 1101000
    module.ROUND_NUM = 411
    module.EXPECTED = 115
    module.APPLY_SCRIPT = BASE / "scripts" / "apply_4050_aug_sep_review_round.py"

    def material_index() -> dict[str, tuple[Path, Path]]:
        result: dict[str, tuple[Path, Path]] = {}
        for input_root in INPUT_ROOTS:
            for input_dir in sorted(p for p in input_root.iterdir() if p.is_dir()):
                report = INIT_ROOT / input_dir.name / "report.json"
                if not report.is_file():
                    continue
                data = json.loads(report.read_text(encoding="utf-8"))
                doi = str((data.get("paper") or {}).get("doi") or "").strip().lower()
                if doi in result:
                    raise RuntimeError(f"输入目录 DOI 重复: {doi}")
                if doi:
                    result[doi] = (input_dir, report)
        return result

    def fetch_rows() -> list[tuple[int, str, str | None]]:
        doi_order = list(material_index())
        rows_by_doi: dict[str, tuple[int, str, str | None]] = {}
        conn = pymssql.connect(**DB)
        try:
            cur = conn.cursor()
            for offset in range(0, len(doi_order), 50):
                chunk = doi_order[offset:offset + 50]
                placeholders = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"SELECT id, doi, report_url FROM yujing_4050_202607 WHERE doi IN ({placeholders})",
                    tuple(chunk),
                )
                for paper_id, doi, report_url in cur.fetchall():
                    key = str(doi or "").strip().lower()
                    if key in rows_by_doi:
                        raise RuntimeError(f"数据库 DOI 重复: {key}")
                    rows_by_doi[key] = (int(paper_id), str(doi or "").strip(), report_url)
        finally:
            conn.close()
        missing = [doi for doi in doi_order if doi not in rows_by_doi]
        if missing:
            raise RuntimeError("数据库缺失 DOI: " + ", ".join(missing))
        return [rows_by_doi[doi] for doi in doi_order]

    original_feed = module.feed
    original_prepare_prompts = module.prepare_prompts

    def evidence_cache_path(item: dict) -> Path:
        identity = {
            "doi": item["doi"],
            "input_dir": str(Path(item["input_dir"]).resolve()),
            "report_json": str(Path(item["report_json"]).resolve()),
        }
        digest = hashlib.sha1(
            json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12]
        slug = str(item["doi"]).replace("/", "_")
        return EVIDENCE_ROOT / f"{slug}_{digest}_evidence.json"

    def feed() -> int:
        result = original_feed()
        papers = []
        for path in sorted((module.ROUND_DIR / "batches").glob("batch_*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            item = data[0]
            item["source_path"] = "input/8月八大刊新增 或 input/9月八大刊新增"
            item["evidence_path"] = str(evidence_cache_path(item))
            path.write_text(json.dumps([item], ensure_ascii=False, indent=2), encoding="utf-8")
            papers.append(item)
        (module.ROUND_DIR / "papers.json").write_text(
            json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result

    def prepare_prompts() -> dict[str, Path]:
        paths = original_prepare_prompts()
        replacements = {
            "yujing_scholar": "yujing_4050_202607",
            "data/input/scholar/8月八大刊新增、9月八大刊新增":
                "data/input/8月八大刊新增 或 data/input/9月八大刊新增",
            "data/input/scholar/caideng`、`huyuhong` 或 `tangyehui":
                "data/input/8月八大刊新增` 或 `data/input/9月八大刊新增",
            "data/input/scholar/caideng、huyuhong 或 tangyehui":
                "data/input/8月八大刊新增 或 data/input/9月八大刊新增",
            "法学理论论文不应机械套用实验论文的样本量和主图要求；应重点核查文本、引文、论证内部一致性及可用材料中的具体异常。": "",
            "法学理论论文应按其研究类型核查文本、引文和论证一致性，不机械要求实验样本量或主图。": "",
        }
        for path in paths.values():
            content = path.read_text(encoding="utf-8")
            for old, new in replacements.items():
                content = content.replace(old, new)
            content = content.replace(
                "## 输出\n",
                "## 输出\n"
                "- 必须同时输出 `dimension_verdicts`，包含 image/data/ref/methodology 四个键，"
                "每个值只能是 `建议高风险` 或 `建议低风险`，并与对应分节文字一致。\n"
                "- `methodology_review` 即使没有发现问题也不能留空；应简要写明已核对的研究设计、"
                "统计、伦理或时间线范围并给出低风险结论。\n",
            )
            content = content.replace(
                '"methodology_review": "中文复核详情或空字符串",',
                '"methodology_review": "中文复核详情（无问题也需写明核查范围和结论）",\n'
                '  "dimension_verdicts": {"image": "建议低风险", "data": "建议低风险", '
                '"ref": "建议低风险", "methodology": "建议低风险"},',
            )
            content = content.replace(
                '"methodology_review": "中文最终复核详情或空字符串",',
                '"methodology_review": "中文最终复核详情（无问题也需写明核查范围和结论）",\n'
                '  "dimension_verdicts": {"image": "建议低风险", "data": "建议低风险", '
                '"ref": "建议低风险", "methodology": "建议低风险"},',
            )
            path.write_text(content, encoding="utf-8")
        return paths

    module.material_index = material_index
    module.fetch_rows = fetch_rows
    module.feed = feed
    module.prepare_prompts = prepare_prompts
    return module


if __name__ == "__main__":
    raise SystemExit(load_runner().main())
