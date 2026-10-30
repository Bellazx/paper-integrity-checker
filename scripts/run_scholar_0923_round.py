#!/usr/bin/env python3
"""Three-stage review round for the 0923 scholar upload."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pymssql


BASE = Path("/home/sjtu/paper-integrity-checker")
SOURCE = BASE / "scripts" / "run_scholar_youjiabin_round.py"
INPUT_ROOT = BASE / "data" / "input" / "scholar" / "0923"
INIT_ROOT = BASE / "data" / "output" / "yujing_scholar" / "init"
ROUND_NAME = "yujing_scholar_0923_round_1"
ROUND_DIR = BASE / "data" / ROUND_NAME
AUTHORS = ("李恬", "丁铭", "张喜寒")
EXPECTED_BY_AUTHOR = {"李恬": 10, "丁铭": 6, "张喜寒": 10}
EXPECTED = sum(EXPECTED_BY_AUTHOR.values())

DB = dict(
    server="10.119.5.44", user="yujing", password="fengxian_YJ514",
    database="lunwenyujing", charset="utf8", timeout=30,
)


def load_runner():
    spec = importlib.util.spec_from_file_location("scholar_0923_shared", SOURCE)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载复审编排模块: {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__file__ = str(Path(__file__).resolve())
    module.ROUND_NAME = ROUND_NAME
    module.ROUND_DIR = ROUND_DIR
    module.CONFIG_DIR = ROUND_DIR / "config"
    module.SCHOLAR = "、".join(AUTHORS)
    module.PROMPT_TAG = "scholar_0923_round1"
    module.INPUT_ROOT = INPUT_ROOT
    module.START_BATCH = 2101000
    module.ROUND_NUM = 421
    module.EXPECTED = EXPECTED
    module.APPLY_SCRIPT = BASE / "scripts" / "apply_scholar_0923_round.py"
    module.DB = DB

    def material_index():
        result = {}
        counts = {}
        for author in AUTHORS:
            author_dir = INPUT_ROOT / author
            if not author_dir.is_dir():
                raise RuntimeError(f"缺少学者目录: {author_dir}")
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
                key = (author, doi)
                if key in result:
                    raise RuntimeError(f"输入目录 DOI 重复: scholar={author} doi={doi}")
                result[key] = (author, input_dir, report)
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
            for (author, doi), (_, input_dir, report) in index.items():
                cur.execute(
                    """SELECT id, doi, report_url, status, review_result, college
                       FROM yujing_scholar
                       WHERE LOWER(doi)=LOWER(%s) AND scholar=%s""",
                    (doi, author),
                )
                found = cur.fetchall()
                if len(found) != 1:
                    raise RuntimeError(
                        f"数据库匹配异常: scholar={author} doi={doi} rows={len(found)}"
                    )
                paper_id, db_doi, report_url, status, review_result, college = found[0]
                if status not in (None, 0) or review_result:
                    raise RuntimeError(
                        f"数据库记录非待复审: scholar={author} doi={doi} "
                        f"status={status} review={review_result}"
                    )
                expected_college = "集成电路学院" if author == "李恬" else "心理学院"
                if str(college or "").strip() != expected_college:
                    raise RuntimeError(
                        f"学院字段异常: scholar={author} doi={doi} college={college!r}"
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
                "slug": input_dir.name,
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
            replacements = {
                "yujing_scholar_caideng_huyuhong_tangyehui_round_1": ROUND_NAME,
                "scholar_three_round1": module.PROMPT_TAG,
                "蔡登、胡玉鸿、唐业辉": "李恬、丁铭、张喜寒",
                "caideng`、`huyuhong` 或 `tangyehui": "0923/李恬、丁铭 或 张喜寒",
                "本批 27 篇": f"本批 {EXPECTED} 篇",
                "data/input/scholar/caideng": "data/input/scholar/0923/李恬",
            }
            for old, new in replacements.items():
                content = content.replace(old, new)
            mandatory = (
                "\n## 本批强制规则\n"
                "- 开始复审前完整阅读 `/home/sjtu/paper-integrity-checker/skills/paper-batch-review/RUNTIME_RULES.md`。\n"
                f"- 本批 {EXPECTED} 篇全部复审，不得仅处理初审高风险论文。\n"
                "- 先读取 batch 中的 evidence_path，逐项核对所有必查项目。\n"
                "- 必须独立阅读论文 PDF、检查所有可用补充材料，并至少核对三张主图与数据/图注的一致性。\n"
                "- 材料缺失本身不是高风险证据；只有具体、可定位的问题才支持高风险。\n"
                "- 最终文本覆盖全部初审高风险发现、确定性数据发现和图文一致性检查，禁止泛泛写“无异常”。\n"
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
