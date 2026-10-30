#!/usr/bin/env python3
"""Three-stage independent review round for scholar 林欣."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pymssql

BASE = Path("/home/sjtu/paper-integrity-checker")
SOURCE = BASE / "scripts" / "run_scholar_youjiabin_round.py"
INPUT_ROOT = BASE / "data/input/scholar/林欣"
INIT_ROOT = BASE / "data/output/yujing_scholar/init"
ROUND_NAME = "yujing_scholar_linxin_round_1"
ROUND_DIR = BASE / "data" / ROUND_NAME
SCHOLAR = "林欣"
EXPECTED = 49
DB = dict(server="10.119.5.44", user="yujing", password="fengxian_YJ514",
          database="lunwenyujing", charset="utf8", timeout=30)


def load_runner():
    spec = importlib.util.spec_from_file_location("scholar_linxin_shared", SOURCE)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载复审编排模块: {SOURCE}")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.__file__ = str(Path(__file__).resolve())
    m.ROUND_NAME = ROUND_NAME
    m.ROUND_DIR = ROUND_DIR
    m.CONFIG_DIR = ROUND_DIR / "config"
    m.SCHOLAR = SCHOLAR
    m.PROMPT_TAG = "scholar_linxin_round1"
    m.INPUT_ROOT = INPUT_ROOT
    m.START_BATCH = 2301000
    m.ROUND_NUM = 424
    m.EXPECTED = EXPECTED
    m.APPLY_SCRIPT = BASE / "scripts/apply_scholar_linxin_round.py"
    m.DB = DB

    def material_index():
        result = {}
        for input_dir in sorted(p for p in INPUT_ROOT.iterdir() if p.is_dir()):
            report = INIT_ROOT / input_dir.name / "report.json"
            if not report.is_file():
                raise RuntimeError(f"缺少初审结果: {report}")
            doi = m.report_doi(report)
            if not doi:
                raise RuntimeError(f"初审 DOI 为空: {report}")
            if doi.replace("/", "_") != input_dir.name.lower():
                raise RuntimeError(f"目录与初审 DOI 不一致: {input_dir.name} vs {doi}")
            if doi in result:
                raise RuntimeError(f"输入目录 DOI 重复: {doi}")
            result[doi] = (input_dir, report)
        if len(result) != EXPECTED:
            raise RuntimeError(f"材料应为 {EXPECTED} 篇，实际为 {len(result)} 篇")
        return result

    def feed():
        batch_dir = ROUND_DIR / "batches"
        batch_dir.mkdir(parents=True, exist_ok=True)
        if list(batch_dir.glob("batch_*.json")):
            raise RuntimeError(f"批次目录已有文件，拒绝重复投递: {batch_dir}")
        index = material_index()
        conn = pymssql.connect(**DB)
        rows = []
        try:
            cur = conn.cursor()
            for doi, (input_dir, report) in index.items():
                cur.execute("""SELECT id, doi, report_url, status, review_result, college
                    FROM yujing_scholar WHERE LOWER(doi)=LOWER(%s) AND scholar=%s""", (doi, SCHOLAR))
                found = cur.fetchall()
                if len(found) != 1:
                    raise RuntimeError(f"数据库匹配异常: doi={doi} rows={len(found)}")
                paper_id, db_doi, report_url, status, review_result, college = found[0]
                if status not in (None, 0) or review_result:
                    raise RuntimeError(f"数据库记录非待复审: doi={doi} status={status} review={review_result}")
                rows.append((int(paper_id), str(db_doi).strip(), report_url, input_dir, report))
        finally:
            conn.close()
        rows.sort(key=lambda x: x[0])
        papers = []
        for offset, (paper_id, doi, report_url, input_dir, report) in enumerate(rows):
            batch_num = m.START_BATCH + offset
            first_dir = report.parent
            item = {"id": paper_id, "doi": doi, "slug": input_dir.name,
                    "scholar": SCHOLAR, "source_path": str(input_dir.relative_to(BASE / "data")),
                    "initial_report_url": report_url, "input_dir": str(input_dir.resolve()),
                    "report_json": str(report.resolve()), "first_check_dir": str(first_dir.resolve()),
                    "first_check_images": str(first_dir / "images") if (first_dir / "images").exists() else None,
                    "first_check_data_tables": str(first_dir / "data_tables") if (first_dir / "data_tables").exists() else None,
                    "first_check_chinese_reports_dir": str(m.CHINESE_REPORT_DIR),
                    "evidence_path": str(ROUND_DIR / "evidence" / f"batch_{batch_num:04d}_evidence.json")}
            (batch_dir / f"batch_{batch_num:04d}.json").write_text(json.dumps([item], ensure_ascii=False, indent=2), encoding="utf-8")
            papers.append(item)
        (ROUND_DIR / "papers.json").write_text(json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8")
        m.log(f"db_matched={len(rows)} added={len(papers)} total={len(papers)} scholar={SCHOLAR} missing=0")
        return 0

    def prepare_prompts():
        m.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        result = {}
        for stage, filename in {"r1":"audit_ai_prompt.txt", "r2":"audit_ai_prompt_reviewer2.txt", "judge":"audit_ai_prompt_judge.txt"}.items():
            content = (m.TEMPLATE_DIR / filename).read_text(encoding="utf-8")
            for old, new in {"yujing_scholar_caideng_huyuhong_tangyehui_round_1": ROUND_NAME,
                             "scholar_three_round1": m.PROMPT_TAG,
                             "蔡登、胡玉鸿、唐业辉": SCHOLAR,
                             "caideng`、`huyuhong` 或 `tangyehui": SCHOLAR,
                             "本批 27 篇": f"本批 {EXPECTED} 篇",
                             "data/input/scholar/caideng": "data/input/scholar/林欣"}.items():
                content = content.replace(old, new)
            mandatory = ("\n## 本批强制规则\n"
                "- 开始复审前完整阅读 `/home/sjtu/paper-integrity-checker/skills/paper-batch-review/RUNTIME_RULES.md`。\n"
                f"- 本批 {EXPECTED} 篇全部复审，不得仅处理初审高风险论文。\n"
                "- 先读取 batch 中的 evidence_path，逐项核对 coverage_manifest.must_address。\n"
                "- 必须独立阅读论文 PDF、检查所有可用补充材料，并至少核对三张主图与数据/图注的一致性。\n"
                "- 材料缺失本身不是高风险证据；只有具体、可定位的问题才支持高风险。\n"
                "- 最终文本覆盖全部初审高风险发现、确定性数据发现和图文一致性检查，禁止泛泛写‘无异常’。\n")
            if "## 本批强制规则" not in content:
                content = content.replace("\n## 输出\n", mandatory + "\n## 输出\n")
            target = m.CONFIG_DIR / filename
            target.write_text(content, encoding="utf-8")
            result[stage] = target
        return result

    m.material_index, m.feed, m.prepare_prompts = material_index, feed, prepare_prompts
    return m


if __name__ == "__main__":
    raise SystemExit(load_runner().main())
