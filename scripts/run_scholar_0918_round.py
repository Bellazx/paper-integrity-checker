#!/usr/bin/env python3
"""Isolated three-stage review round for the 2026-09-18 scholar upload."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pymssql


BASE = Path("/home/sjtu/paper-integrity-checker")
SOURCE = BASE / "scripts" / "run_scholar_youjiabin_round.py"
INPUT_ROOT = BASE / "data" / "input" / "scholar" / "0918"
INIT_ROOT = BASE / "data" / "output" / "yujing_scholar" / "init"
ROUND_NAME = "yujing_scholar_0918_round_1"
ROUND_DIR = BASE / "data" / ROUND_NAME
AUTHORS = ("方懿", "卢彬", "邵伟增")
EXPECTED_BY_AUTHOR = {"方懿": 31, "卢彬": 34, "邵伟增": 120}
EXPECTED = sum(EXPECTED_BY_AUTHOR.values())

DB = dict(
    server="10.119.5.44", user="yujing", password="fengxian_YJ514",
    database="lunwenyujing", charset="utf8", timeout=30,
)


def load_runner():
    spec = importlib.util.spec_from_file_location("scholar_0918_shared", SOURCE)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载批次编排模块: {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__file__ = str(Path(__file__).resolve())
    module.ROUND_NAME = ROUND_NAME
    module.ROUND_DIR = ROUND_DIR
    module.CONFIG_DIR = ROUND_DIR / "config"
    module.SCHOLAR = "、".join(AUTHORS)
    module.PROMPT_TAG = "scholar_0918_round1"
    module.INPUT_ROOT = INPUT_ROOT
    module.START_BATCH = 1601000
    module.ROUND_NUM = 416
    module.EXPECTED = EXPECTED
    module.APPLY_SCRIPT = BASE / "scripts" / "apply_scholar_0918_round.py"
    module.DB = DB

    def material_index():
        result = {}
        counts = {}
        actual_authors = {p.name for p in INPUT_ROOT.iterdir() if p.is_dir()}
        if actual_authors != set(AUTHORS):
            raise RuntimeError(
                f"学者目录不一致: expected={sorted(AUTHORS)} actual={sorted(actual_authors)}"
            )
        for author in AUTHORS:
            author_dir = INPUT_ROOT / author
            counts[author] = 0
            for input_dir in sorted(p for p in author_dir.iterdir() if p.is_dir()):
                report = INIT_ROOT / input_dir.name / "report.json"
                if not report.is_file():
                    raise RuntimeError(f"缺少初审结果: {report}")
                doi = module.report_doi(report)
                if not doi:
                    raise RuntimeError(f"初审 DOI 为空: {report}")
                if doi.replace("/", "_") != input_dir.name.lower():
                    raise RuntimeError(
                        f"目录与初审 DOI 不一致: dir={input_dir.name} report={doi}"
                    )
                if doi in result:
                    raise RuntimeError(f"输入目录 DOI 重复: {doi}")
                result[doi] = (author, input_dir, report)
                counts[author] += 1
        if counts != EXPECTED_BY_AUTHOR:
            raise RuntimeError(f"各学者论文数异常: expected={EXPECTED_BY_AUTHOR} actual={counts}")
        return result

    def feed():
        batch_dir = ROUND_DIR / "batches"
        batch_dir.mkdir(parents=True, exist_ok=True)
        if list(batch_dir.glob("batch_*.json")):
            raise RuntimeError(f"批次目录已有文件，拒绝重复投递: {batch_dir}")
        index = material_index()
        if len(index) != EXPECTED:
            raise RuntimeError(f"论文材料应为 {EXPECTED} 篇，实际为 {len(index)} 篇")

        conn = pymssql.connect(**DB)
        try:
            cur = conn.cursor()
            rows = []
            for doi, (author, input_dir, report) in index.items():
                cur.execute(
                    """SELECT id, doi, report_url, status
                       FROM yujing_scholar WHERE doi=%s AND scholar=%s""",
                    (doi, author),
                )
                found = cur.fetchall()
                if len(found) != 1:
                    raise RuntimeError(
                        f"数据库匹配异常: scholar={author} doi={doi} rows={len(found)}"
                    )
                paper_id, db_doi, report_url, status = found[0]
                if status not in (None, 0):
                    raise RuntimeError(
                        f"数据库记录非待复审: scholar={author} doi={doi} status={status}"
                    )
                rows.append((int(paper_id), str(db_doi).strip(), report_url,
                             author, input_dir, report))
        finally:
            conn.close()

        rows.sort(key=lambda row: row[0])
        papers = []
        author_counts = {author: 0 for author in AUTHORS}
        for offset, (paper_id, doi, report_url, author, input_dir, report) in enumerate(rows):
            batch_num = module.START_BATCH + offset
            first_dir = report.parent
            item = {
                "id": paper_id,
                "doi": doi,
                "slug": doi.replace("/", "_"),
                "scholar": author,
                "source_path": str(input_dir.relative_to(BASE / "data")),
                "initial_report_url": report_url,
                "input_dir": str(input_dir.resolve()),
                "report_json": str(report.resolve()),
                "first_check_dir": str(first_dir.resolve()),
                "first_check_images": str(first_dir / "images") if (first_dir / "images").exists() else None,
                "first_check_data_tables": str(first_dir / "data_tables") if (first_dir / "data_tables").exists() else None,
                "first_check_chinese_reports_dir": str(module.CHINESE_REPORT_DIR),
                "evidence_path": str(ROUND_DIR / "evidence" / f"batch_{batch_num:04d}_evidence.json"),
            }
            (batch_dir / f"batch_{batch_num:04d}.json").write_text(
                json.dumps([item], ensure_ascii=False, indent=2), encoding="utf-8"
            )
            papers.append(item)
            author_counts[author] += 1
        if author_counts != EXPECTED_BY_AUTHOR:
            raise RuntimeError(f"投递计数异常: expected={EXPECTED_BY_AUTHOR} actual={author_counts}")
        (ROUND_DIR / "papers.json").write_text(
            json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        module.log(
            f"db_matched={len(rows)} added={len(papers)} total={len(papers)} "
            f"authors={author_counts} missing=0"
        )
        return 0

    def prepare_prompts():
        module.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        names = {
            "r1": "audit_ai_prompt.txt",
            "r2": "audit_ai_prompt_reviewer2.txt",
            "judge": "audit_ai_prompt_judge.txt",
        }
        result = {}
        for stage, filename in names.items():
            content = (module.TEMPLATE_DIR / filename).read_text(encoding="utf-8")
            content = content.replace(
                "yujing_scholar_caideng_huyuhong_tangyehui_round_1", ROUND_NAME
            )
            content = content.replace("scholar_three_round1", module.PROMPT_TAG)
            content = content.replace("蔡登、胡玉鸿、唐业辉", "方懿、卢彬、邵伟增")
            content = content.replace(
                "caideng`、`huyuhong` 或 `tangyehui", "0918/<学者姓名>"
            )
            content = content.replace("本批 27 篇", f"本批 {EXPECTED} 篇")
            content = content.replace(
                "data/input/scholar/caideng", "data/input/scholar/0918/方懿"
            )
            content = content.replace(
                "2. 读取 evidence_path 和 report_json；若 evidence 缺失则继续按可用材料审核。",
                "2. 先读取 evidence_path 和 report_json；evidence 应已预生成，若暂未生成则等待后重试，"
                "禁止在缺少证据清单时给出低风险。必须逐项覆盖 coverage_manifest.must_address。",
            )
            mandatory = (
                "\n## 证据清单强制要求\n"
                "- 先读取 batch 中的 `evidence_path`，必须逐项核对 `coverage_manifest.must_address`。\n"
                "- 若证据文件暂未生成，等待后重试；不得在缺少该清单时输出低风险。\n"
                "- 最终文本必须明确覆盖所有 HIGH 发现、确定性数据发现和主图图文一致性核查。\n"
                "- 不得用图中可见箭头/散点数量直接反推计算网格或原始样本量；必须先排除绘图抽稀、空间配准和网格分辨率差异。\n"
                "- 参考文献在 PDF 末页中途截断时，必须先判断下载/上传文件是否缺页；材料截断本身不得当作论文高风险证据。\n"
            )
            if "## 证据清单强制要求" not in content:
                content = content.replace("\n## 输出\n", mandatory + "\n## 输出\n")
            target = module.CONFIG_DIR / filename
            target.write_text(content, encoding="utf-8")
            result[stage] = target
        return result

    module.material_index = material_index
    module.feed = feed
    module.prepare_prompts = prepare_prompts
    return module


if __name__ == "__main__":
    raise SystemExit(load_runner().main())
