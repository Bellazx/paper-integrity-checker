#!/usr/bin/env python3
"""Isolated three-stage review round for Wang Jiulin's scholar upload."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pymssql


BASE = Path("/home/sjtu/paper-integrity-checker")
SOURCE = BASE / "scripts" / "run_scholar_youjiabin_round.py"
INPUT_ROOT = BASE / "data" / "input" / "scholar"
INIT_ROOT = BASE / "data" / "output" / "yujing_scholar" / "init"
ROUND_NAME = "yujing_scholar_wangjiulin_round_1"
ROUND_DIR = BASE / "data" / ROUND_NAME
SCHOLAR = "王久林"
EXPECTED = 10

DB = dict(
    server="10.119.5.44", user="yujing", password="fengxian_YJ514",
    database="lunwenyujing", charset="utf8", timeout=30,
)


def load_runner():
    spec = importlib.util.spec_from_file_location("scholar_wangjiulin_shared", SOURCE)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载批次编排模块: {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__file__ = str(Path(__file__).resolve())
    module.ROUND_NAME = ROUND_NAME
    module.ROUND_DIR = ROUND_DIR
    module.CONFIG_DIR = ROUND_DIR / "config"
    module.SCHOLAR = SCHOLAR
    module.PROMPT_TAG = "scholar_wangjiulin_round1"
    module.INPUT_ROOT = INPUT_ROOT
    module.START_BATCH = 1801000
    module.ROUND_NUM = 418
    module.EXPECTED = EXPECTED
    module.APPLY_SCRIPT = BASE / "scripts" / "apply_scholar_wangjiulin_round.py"
    module.DB = DB

    def material_index():
        author_dir = INPUT_ROOT / SCHOLAR
        if not author_dir.is_dir():
            raise RuntimeError(f"缺少学者目录: {author_dir}")
        result = {}
        for input_dir in sorted(path for path in author_dir.iterdir() if path.is_dir()):
            report = INIT_ROOT / input_dir.name / "report.json"
            if not report.is_file():
                raise RuntimeError(f"缺少初审结果: {report}")
            doi = module.report_doi(report)
            if not doi:
                raise RuntimeError(f"初审 DOI 为空: {report}")
            if doi in result:
                raise RuntimeError(f"输入目录 DOI 重复: {doi}")
            result[doi] = (input_dir, report)
        if len(result) != EXPECTED:
            raise RuntimeError(f"论文材料应为 {EXPECTED} 篇，实际为 {len(result)} 篇")
        return result

    def feed():
        batch_dir = ROUND_DIR / "batches"
        batch_dir.mkdir(parents=True, exist_ok=True)
        if list(batch_dir.glob("batch_*.json")):
            raise RuntimeError(f"批次目录已有文件，拒绝重复投递: {batch_dir}")
        index = material_index()

        conn = pymssql.connect(**DB)
        try:
            cur = conn.cursor()
            rows = []
            for doi, (input_dir, report) in index.items():
                cur.execute(
                    """SELECT id, doi, report_url, status, review_result
                       FROM yujing_scholar WHERE LOWER(doi)=LOWER(%s) AND scholar=%s""",
                    (doi, SCHOLAR),
                )
                found = cur.fetchall()
                if len(found) != 1:
                    raise RuntimeError(f"数据库匹配异常: scholar={SCHOLAR} doi={doi} rows={len(found)}")
                paper_id, db_doi, report_url, status, review_result = found[0]
                if status not in (None, 0) or review_result:
                    raise RuntimeError(
                        f"数据库记录非待复审: doi={doi} status={status} review={review_result}"
                    )
                rows.append((int(paper_id), str(db_doi).strip(), report_url, input_dir, report))
        finally:
            conn.close()

        rows.sort(key=lambda row: row[0])
        papers = []
        for offset, (paper_id, doi, report_url, input_dir, report) in enumerate(rows):
            batch_num = module.START_BATCH + offset
            first_dir = report.parent
            item = {
                "id": paper_id,
                "doi": doi,
                "slug": input_dir.name,
                "scholar": SCHOLAR,
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
        (ROUND_DIR / "papers.json").write_text(
            json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        module.log(f"db_matched={len(rows)} added={len(papers)} total={len(papers)} scholar={SCHOLAR} missing=0")
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
            content = content.replace("蔡登、胡玉鸿、唐业辉", SCHOLAR)
            content = content.replace(
                "caideng`、`huyuhong` 或 `tangyehui", "王久林"
            )
            content = content.replace("本批 27 篇", f"本批 {EXPECTED} 篇")
            content = content.replace("data/input/scholar/caideng", "data/input/scholar/王久林")
            content = content.replace(
                "2. 读取 evidence_path 和 report_json；若 evidence 缺失则继续按可用材料审核。",
                "2. 先读取 evidence_path 和 report_json；evidence 应已预生成，若暂未生成则等待后重试，"
                "禁止在缺少证据清单时给出低风险。必须逐项覆盖 coverage_manifest.must_address。",
            )
            mandatory = (
                "\n## 本批强制规则\n"
                "- 开始复审前完整阅读 `/home/sjtu/paper-integrity-checker/skills/paper-batch-review/RUNTIME_RULES.md`。\n"
                "- 先读取 batch 中的 `evidence_path`，逐项核对 `coverage_manifest.must_address`。\n"
                "- 必须独立阅读论文 PDF、检查所有可用补充材料，并至少核对三张主图与数据/图注的一致性。\n"
                "- 材料缺失本身不是高风险证据；只有具体、可定位的问题才支持高风险。\n"
                "- 最终文本覆盖全部 HIGH 发现、确定性数据发现和图文一致性检查，禁止泛泛写“无异常”。\n"
            )
            if "## 本批强制规则" not in content:
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
