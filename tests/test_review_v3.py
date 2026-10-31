"""Offline tests for review engine v3 (fake CLI, no model or network)."""
import asyncio
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_v3 import checklist, config, engine, ledger, memory, session  # noqa: E402

FAKE = ROOT / "tests" / "review_v3_fake_claude.py"
PV3 = ROOT / "review_v3" / "bin" / "pv3"


# ------------------------------------------------------------------ fixtures

def _make_paper(tmp: Path) -> dict:
    import fitz
    import openpyxl
    paper = tmp / "input" / "paper"
    paper.mkdir(parents=True)
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((50, 72), "Fig. 1: Cell viability in two groups (n = 8).")
    page.insert_text((50, 100), "Methods: three independent experiments were performed.")
    page = pdf.new_page()
    page.insert_text((50, 72), "References")
    page.insert_text((50, 92), "1. Smith, J. A study. Nature 1, 1-2 (2020).")
    pdf.save(str(paper / "paper.pdf"))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["time", "ctrl", "treat"])
    for i in range(8):
        ws.append([i, 1.0 + i * 0.1, 1.0 + i * 0.1])
    wb.save(str(paper / "data.xlsx"))
    report = {"paper": {"title": "A test paper", "doi": "10.1/test"},
              "image_duplicates": [], "image_splicing": [],
              "reference_issues": [{"ref_number": 1, "ref_text": "Smith", "issue_type": "low_match_score",
                                    "severity": "medium", "details": {}}],
              "data_anomalies": [{"test": "cross_group_duplicate", "severity": "high",
                                  "location": "data.xlsx / Sheet1 / ctrl vs treat"}]}
    report_path = tmp / "report.json"
    report_path.write_text(json.dumps(report))
    evidence = {
        "doi": "10.1/test", "input_dir": str(paper), "paper": {"title": "A test paper", "filepath": str(paper / "paper.pdf")},
        "inventory": [{"file": "data.xlsx", "path": str(paper / "data.xlsx"),
                       "sheets": [{"sheet": "Sheet1", "n_rows": 9, "columns": [{"header": "time", "category": "independent_variable"}]}]}],
        "main_figures": [],
        "high_findings": [{"dim": "data", "test": "cross_group_duplicate", "location": "data.xlsx / Sheet1 / ctrl vs treat"}],
        "deterministic_findings": {},
        "coverage_manifest": {"must_address": ["high::data::0"], "counts": {"total_must_address": 1}},
    }
    evidence_path = tmp / "evidence.json"
    evidence_path.write_text(json.dumps(evidence))
    return {"input_dir": str(paper), "report": str(report_path), "evidence": str(evidence_path), "evidence_obj": evidence}


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    wrapper = tmp_path / "claude"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    scenario = tmp_path / "scenario.json"
    log = tmp_path / "calls.jsonl"
    log.write_text("")
    monkeypatch.setenv("CLAUDE_CLI", str(wrapper))
    monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", str(scenario))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    memdir = tmp_path / "memory"
    memdir.mkdir()
    (memdir / "audit-shared-axis.md").write_text(
        "---\nname: audit-shared-axis\ndescription: 共享横轴列逐值相同属假阳性，须先读列头确认自变量\n---\n"
        "**How to apply**: 打开 xlsx 读列头与单位\n")
    monkeypatch.setattr(config, "MEMORY_DIR", memdir)
    monkeypatch.setattr(config, "MEMORY_INBOX", tmp_path / "inbox")
    monkeypatch.setattr(config, "CHUNK_TIMEOUT_SECONDS", 60)

    def configure(**behaviour):
        scenario.write_text(json.dumps(behaviour))

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]

    return configure, calls


def _run(paper, tmp_path, monkeypatch, resume=False):
    async def fake_evidence(*_args):
        return paper["evidence"]
    monkeypatch.setattr(engine, "_evidence", fake_evidence)
    return asyncio.run(engine.run_review("10.1/test", paper["report"], paper["input_dir"],
                                         str(tmp_path / "out"), resume=resume))


# ------------------------------------------------------------------ ledger

def test_ledger_rejects_unsupported_conclusions(tmp_path):
    path = tmp_path / "l.json"
    ledger.create(path, {"shard": "s"}, [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}])
    applied, errors = ledger.update(path, [
        {"id": "a", "status": "cleared", "basis": "短"},
        {"id": "b", "status": "issue", "severity": "substantive", "location": "Fig. 1",
         "issue_type": "数据复用", "finding": "两列相同", "why": "影响比较", "basis": "逐值比对"},
        {"id": "new:x", "status": "issue", "severity": "minor"},
    ], "t")
    assert applied == 1
    assert any("a: cleared" in e for e in errors)
    assert any("new:x" in e and "dimension" in e for e in errors)
    data = ledger.load(path)
    assert ledger.counts(data)["issue"] == 1 and ledger.counts(data)["open"] == 1


def test_ledger_create_is_idempotent_for_resume(tmp_path):
    path = tmp_path / "l.json"
    ledger.create(path, {}, [{"id": "a"}])
    ledger.update(path, [{"id": "a", "status": "not_applicable", "basis": "未提供源数据，已核对图注与正文"}], "t")
    ledger.create(path, {}, [{"id": "a"}])
    assert ledger.load(path)["items"]["a"]["status"] == "not_applicable"


# ------------------------------------------------------------------ checklist

def test_checklist_groups_same_test_and_keeps_members():
    high = [{"dim": "data", "test": "decimal_injection", "location": f"f.xlsx / s1 / c{i}"} for i in range(30)]
    high.append({"dim": "image", "match_type": "full_duplicate", "pages": [1, 2]})
    evidence = {"high_findings": high,
                "coverage_manifest": {"must_address": [f"high::{h['dim']}::{i}" for i, h in enumerate(high)]}}
    shards = checklist.build_shards(evidence, {"main_figure_numbers": ["1", "2"], "captions": [], "data_files": []})
    data_items = [i for s in shards if s["kind"] == "data" for i in s["items"]]
    group = next(i for i in data_items if i["id"].startswith("group::"))
    assert len(group["members"]) == 30
    covered = {m for i in data_items + [i for s in shards for i in s["items"]] for m in (i.get("members") or [i["id"]])}
    assert all(f"high::{h['dim']}::{i}" in covered for i, h in enumerate(high))
    kinds = {s["kind"] for s in shards}
    assert {"image", "data", "consistency", "ref", "methodology"} <= kinds


def test_memory_search_ranks_relevant_lesson(tmp_path):
    (tmp_path / "audit-a.md").write_text("---\nname: a\ndescription: 参考文献 DOI 跨行粘连页码的判据\n---\nbody")
    (tmp_path / "audit-b.md").write_text("---\nname: b\ndescription: 显微图 SIFT 模板假阳性判据\n---\nbody")
    hits = memory.search("SIFT 显微图 局部匹配", "image", memory_dir=tmp_path)
    assert hits and hits[0]["name"] == "b"


# ------------------------------------------------------------------ pv3

def test_pv3_reads_dossier_and_updates_ledger(tmp_path):
    paper = _make_paper(tmp_path)
    workdir = tmp_path / "work"
    from review_v3 import dossier
    index = dossier.build(workdir, "10.1/test", paper["input_dir"], paper["report"], paper["evidence_obj"])
    assert index["main_figure_numbers"] == ["1"]
    ledger.create(workdir / "l.json", {"shard": "x"}, [{"id": "a", "title": "A"}])
    env = {**os.environ, "PV3_WORKDIR": str(workdir), "PV3_LEDGER": str(workdir / "l.json"), "PV3_ACTOR": "t"}
    out = subprocess.run([str(PV3), "find", "n = 8"], env=env, capture_output=True, text=True)
    assert "main p1" in out.stdout
    out = subprocess.run([str(PV3), "colcmp", "data.xlsx", "Sheet1", "B", "C"], env=env, capture_output=True, text=True)
    assert "逐值完全相同 8 对" in out.stdout
    out = subprocess.run([str(PV3), "refs", "1"], env=env, capture_output=True, text=True)
    assert "Smith" in out.stdout
    out = subprocess.run([str(PV3), "ledger", "update"], env=env, capture_output=True, text=True,
                         input=json.dumps([{"id": "a", "status": "cleared", "basis": "核对 Sheet1 列头为时间轴，属共享横轴"}]))
    assert out.returncode == 0 and ledger.load(workdir / "l.json")["items"]["a"]["status"] == "cleared"


# ------------------------------------------------------------------ sessions

def _spec(tmp_path, ledger_path, **kw):
    return session.SessionSpec(name="s", workdir=tmp_path, prompt="p", actor="investigator:x",
                               ledger_path=ledger_path, max_chunks=kw.pop("max_chunks", 10),
                               chunk_turns=5, continue_prompt=lambda s: "继续", **kw)


def test_session_extends_while_progressing(tmp_path, fake_cli):
    configure, calls = fake_cli
    configure(investigator="one_per_call")
    path = tmp_path / "l.json"
    ledger.create(path, {}, [{"id": f"i{n}", "title": "t"} for n in range(4)])
    outcome = asyncio.run(session.run(_spec(tmp_path, path)))
    assert outcome.status == "done"
    made = calls()
    assert len(made) == 4 and not made[0]["resume"] and all(c["resume"] for c in made[1:])
    assert {c["model"] for c in made} == {config.MODEL}
    settings = {c["settings"] for c in made}
    assert len(settings) == 1
    generated = json.loads(Path(settings.pop()).read_text())
    assert "guard.py" in generated["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    if Path(config.SETTINGS_FILE).is_file():
        assert generated["env"]["ANTHROPIC_MODEL"] == json.loads(Path(config.SETTINGS_FILE).read_text())["env"]["ANTHROPIC_MODEL"]


def test_session_stops_when_no_progress(tmp_path, fake_cli):
    configure, calls = fake_cli
    configure(investigator="noop")
    path = tmp_path / "l.json"
    ledger.create(path, {}, [{"id": "i", "title": "t"}])
    outcome = asyncio.run(session.run(_spec(tmp_path, path)))
    assert outcome.status == "stalled"
    assert len(calls()) == config.NO_PROGRESS_CHUNKS + 1  # first chunk is orientation


def test_session_retries_transport_failure_without_spending_chunk(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    configure(investigator="clear_all", transport_fail_first=True)
    monkeypatch.setattr(config, "RETRY_BACKOFF_SECONDS", 0)
    path = tmp_path / "l.json"
    ledger.create(path, {}, [{"id": "i", "title": "t"}])
    outcome = asyncio.run(session.run(_spec(tmp_path, path)))
    assert outcome.status == "done"
    state = json.loads((tmp_path / "sessions" / "s.json").read_text())
    assert state["chunks"] == 1 and len(calls()) == 2


def test_session_resume_does_not_refund_budget(tmp_path, fake_cli):
    configure, calls = fake_cli
    configure(investigator="noop")
    path = tmp_path / "l.json"
    ledger.create(path, {}, [{"id": "i", "title": "t"}])
    asyncio.run(session.run(_spec(tmp_path, path, max_chunks=1)))
    asyncio.run(session.run(_spec(tmp_path, path, max_chunks=1)))
    assert len(calls()) == 1


# ------------------------------------------------------------------ engine

def test_engine_clean_paper_publishes_low_risk(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    configure(investigator="clear_all", challenger="clear_all")
    paper = _make_paper(tmp_path)
    result = _run(paper, tmp_path, monkeypatch)
    assert result.get("trigger") != "review_error", result
    assert result["result"] == "低风险" and result["verdict"] == "建议低风险"
    assert set(result["dimension_verdicts"].values()) == {"建议低风险"}
    assert result["_coverage_gaps"] == [] and result["_engine"] == "v3"
    roles = [c["actor"].split(":")[0] for c in calls()]
    assert "arbiter" not in roles and roles.count("writer") == 1
    assert {c["tools"] for c in calls() if c["actor"] == "writer"} == {""}


def test_engine_confirmed_issue_is_high_risk_with_bound_evidence(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    shot = tmp_path / "shot.png"
    from PIL import Image
    Image.new("RGB", (40, 30), "white").save(shot)
    configure(investigator={"issue": {"high::data::0": "substantive"}},
              challenger={"issue": {"high::data::0": "substantive"}},
              calibrator={"issue": {"high::data::0": "substantive"}})
    paper = _make_paper(tmp_path)
    result = _run(paper, tmp_path, monkeypatch)
    assert result["result"] == "高风险", result
    assert result["dimension_verdicts"]["data"] == "建议高风险"
    assert result["dimension_verdicts"]["image"] == "建议低风险"
    assert "（1）问题位置" in result["data_review"]


def test_engine_dispute_goes_to_arbitration_and_records_lesson(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    configure(investigator={"issue": {"high::data::0": "substantive"}}, challenger="clear_all", arbiter="clear_all")
    paper = _make_paper(tmp_path)
    result = _run(paper, tmp_path, monkeypatch)
    assert result["result"] == "低风险", result
    assert any(c["actor"].startswith("arbiter") for c in calls())
    lessons = (tmp_path / "inbox" / "lessons.jsonl").read_text()
    assert "high::data::0" in lessons


def test_engine_minor_issue_does_not_raise_risk(tmp_path, fake_cli, monkeypatch):
    configure, _ = fake_cli
    configure(investigator={"issue": {"high::data::0": "minor"}}, challenger={"issue": {"high::data::0": "minor"}},
              calibrator={"issue": {"high::data::0": "minor"}})
    paper = _make_paper(tmp_path)
    result = _run(paper, tmp_path, monkeypatch)
    # Minor issues are listed once as corrections, not as numbered risk points.
    assert result["result"] == "低风险" and "另需作者更正" in result["data_review"]
    assert "问题位置" not in result["data_review"]


def test_engine_unfinished_checks_are_disclosed_not_blocking(tmp_path, fake_cli, monkeypatch):
    configure, _ = fake_cli
    configure(investigator="noop", challenger="noop", arbiter="noop")
    paper = _make_paper(tmp_path)
    result = _run(paper, tmp_path, monkeypatch)
    # Still unverified after the completion pass: the verdict rests on what was
    # verified and the report says which screening hits were not checked.
    assert result["trigger"] != "review_error" and result["result"] == "低风险"
    assert "未能完成" in result["data_review"] or "未完成" in result["data_review"]


def test_engine_resume_returns_validated_final_without_model_calls(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    configure(investigator="clear_all", challenger="clear_all")
    paper = _make_paper(tmp_path)
    first = _run(paper, tmp_path, monkeypatch)
    count = len(calls())
    second = _run(paper, tmp_path, monkeypatch, resume=True)
    assert len(calls()) == count
    assert second["result"] == first["result"] and second["_review_workdir"] == first["_review_workdir"]


# ------------------------------------------------------------------ independence

def _guard(tool, payload, env):
    guard = ROOT / "review_v3" / "tools" / "guard.py"
    return subprocess.run([sys.executable, str(guard)], input=json.dumps({"tool_name": tool, "tool_input": payload}),
                          capture_output=True, text=True, env={**os.environ, **env})


def test_guard_blocks_prior_verdicts_and_code_but_allows_paper(tmp_path):
    work = tmp_path / "data" / "output" / "tasks" / "t1" / "review_v3" / "run"
    paper = tmp_path / "data" / "input" / "p1"
    env = {"PV3_WORKDIR": str(work), "PV3_INPUT_DIR": str(paper), "PV3_EXTRA_DIRS": str(tmp_path / "ann"),
           "PAPER_INTEGRITY_ROOT": str(ROOT)}
    allowed = [("Bash", {"command": f"pv3 sheet {paper}/data.xlsx Sheet1"}),
               ("Bash", {"command": f"ls {work}/dossier/renders"}),
               ("Bash", {"command": f"pv3 view {tmp_path}/ann/sift_pair_000.png"}),
               ("Read", {"file_path": f"{work}/dossier/renders/main_p001.png"}),
               ("Bash", {"command": "python3 /home/library/.claude/skills/paper-batch-review/scripts/forensic_checks.py verify-stats --input /tmp/t.json"})]
    blocked = [("Bash", {"command": "cat /home/library/.claude/projects/-home-sjtu-paper-integrity-checker/memory/audit-x.md"}),
               ("Bash", {"command": f"ls {ROOT}/data/output/yujing_scholar/review_v2/"}),
               ("Bash", {"command": "pdftotext review_10.1016_j.jscai.2022.100399.pdf -"}),
               ("Bash", {"command": f"sed -n 1,50p {ROOT}/review_v3/engine.py"}),
               ("Bash", {"command": f"cat {work}/../../review_artifacts/x_state.json"}),
               ("Read", {"file_path": f"{ROOT}/modules/data_checker.py"}),
               ("Read", {"file_path": f"{paper}/../other/review_artifacts/state.json"})]
    for tool, payload in allowed:
        assert _guard(tool, payload, env).returncode == 0, payload
    for tool, payload in blocked:
        assert _guard(tool, payload, env).returncode == 2, payload


def test_memory_never_returns_notes_about_the_reviewed_paper(tmp_path):
    (tmp_path / "audit-jscai-2022-100399-x.md").write_text(
        "---\nname: audit-jscai-2022-100399-x\ndescription: JSCAI 表3 两列全同 判为假阳性 低风险\n---\n表3 两列全同")
    (tmp_path / "audit-general.md").write_text(
        "---\nname: g\ndescription: Word 表格合并单元格造成两列全同的假阳性判据\n---\n表3 两列全同")
    hits = memory.search("表3 两列全同 合并单元格", "data", memory_dir=tmp_path, exclude_doi="10.1016/j.jscai.2022.100399")
    assert [h["name"] for h in hits] == ["g"]
    assert "100399" in memory.doi_markers("10.1016/j.jscai.2022.100399")


# ------------------------------------------------------------------ time boxes

def test_session_wraps_up_when_phase_time_box_closes(tmp_path, fake_cli):
    import time
    configure, calls = fake_cli
    configure(investigator="one_per_call", delay=1.0)
    path = tmp_path / "l.json"
    ledger.create(path, {}, [{"id": f"i{n}", "title": "t"} for n in range(5)])
    spec = _spec(tmp_path, path)
    spec.soft_deadline = time.monotonic() + 1.5
    spec.wrapup_prompt = lambda state: "收尾"
    outcome = asyncio.run(session.run(spec))
    made = calls()
    assert outcome.status == "wrapped"
    assert made[-1]["turns"] == str(config.WRAPUP_TURNS) and made[-1]["resume"]
    assert len(made) < 5


def test_engine_challenges_only_items_that_need_it(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    configure(investigator="clear_all", challenger="clear_all")
    result = _run(_make_paper(tmp_path), tmp_path, monkeypatch)
    assert result["result"] == "低风险"
    challenged = {c["actor"] for c in calls() if c["actor"].startswith("challenger")}
    assert challenged == {"challenger:data"}  # the HIGH data finding; clean synthetic checks are not re-run


def test_engine_open_synthetic_check_is_disclosed_not_blocking(tmp_path, fake_cli, monkeypatch):
    configure, _ = fake_cli
    configure(investigator="clear_all", challenger="clear_all", arbiter="noop", **{"investigator:stats": "noop",
              "challenger:stats": "noop"})
    monkeypatch.setattr(config, "MAX_CHUNKS_ARBITRATE", 1)
    result = _run(_make_paper(tmp_path), tmp_path, monkeypatch)
    assert result.get("trigger") != "review_error", result
    assert result["result"] == "低风险"


def test_engine_calibration_can_downgrade_to_minor(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    configure(investigator={"issue": {"high::data::0": "substantive"}},
              challenger={"issue": {"high::data::0": "substantive"}},
              calibrator={"issue": {"high::data::0": "minor"}})
    result = _run(_make_paper(tmp_path), tmp_path, monkeypatch)
    assert result["result"] == "低风险" and "另需作者更正" in result["data_review"]
    assert any(c["actor"] == "calibrator" for c in calls())
    finals = json.loads(Path(result["_review_workdir"], "finals.json").read_text())
    assert finals["finals"]["high::data::0"]["calibrated_from"]["severity"] == "substantive"


# ------------------------------------------------------------------ entry points

def test_api_entry_dispatches_to_v3_only_when_enabled(monkeypatch):
    import api.services.review as review_module
    seen = {}

    async def fake_run(doi, report, input_dir, output_dir, resume=False):
        seen["call"] = (doi, resume)
        return {"result": "低风险"}
    monkeypatch.setattr(engine, "run_review", fake_run)
    monkeypatch.setenv("REVIEW_ENGINE", "v3")
    assert asyncio.run(review_module.run_review_single("10.1/x", "r", "i", "o", resume=True)) == {"result": "低风险"}
    assert seen["call"] == ("10.1/x", True)


def test_batch_manifest_review_and_resume(tmp_path, fake_cli, monkeypatch):
    from review_v3 import batch
    configure, calls = fake_cli
    configure(investigator="clear_all", challenger="clear_all")
    paper = _make_paper(tmp_path)

    async def fake_evidence(*_args):
        return paper["evidence"]
    monkeypatch.setattr(engine, "_evidence", fake_evidence)
    monkeypatch.setattr(batch, "ROUNDS_DIR", tmp_path / "rounds")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps([{"doi": "10.1/test", "input_dir": paper["input_dir"], "report_json": paper["report"]}]))
    args = type("A", (), dict(round="t", manifest=str(manifest), concurrency=1, max_attempts=2, no_publish=True,
                              apply=False, watch=0, table=None, scholar="", namespace=None))()
    asyncio.run(batch.run(args))
    state = json.loads((tmp_path / "rounds" / "t" / "state.json").read_text())
    record = state["papers"]["10.1/test"]
    assert record["status"] == "reviewed" and record["result"] == "低风险"
    count = len(calls())
    asyncio.run(batch.run(args))  # a finished paper is not reviewed again
    assert len(calls()) == count


# ------------------------------------------------------------------ reference precheck

def test_refcheck_types_and_split_dois(tmp_path, monkeypatch):
    from review_v3 import refcheck
    text = "\n".join([
        "References",
        "1. [n. d.]. ACM Digital Library. Retrieved November 1, 2025 from https://dl.acm.org/",
        "2. A. Author. 2024. A model. arXiv:2401.00001",
        "3. B. Author. 2025. Split title. IEEE Trans. 1, 2 (2025). DOI:https://doi.org/10.1109/TSE.",
        "2025.3564599",
        "4. C. Author. 2014. Guidelines. DOI:https://doi.org/10.1145/2601248.2601268 ACM Comput. Surv., Vol. 58",
        "Supplementary data",
        "5. not a reference",
    ])
    calls = []

    def fake_get(url, cache_dir):
        calls.append(url)
        if url.endswith("10.1109/TSE.2025.3564599") or url.endswith("10.1145/2601248.2601268"):
            title = "Split title" if "TSE" in url else "Guidelines"
            return {"DOI": "x", "title": [title], "author": [{"family": "Author"}]}, "ok"
        return None, "not_found"
    monkeypatch.setattr(refcheck, "_get", fake_get)
    report = refcheck.run(text, tmp_path / "r.json")
    by_num = {r["num"]: r for r in report["results"]}
    assert report["entries"] == 4  # parsing stops at the supplementary section
    assert by_num[1]["status"] == by_num[2]["status"] == "not_indexed_type"
    assert by_num[3]["status"] == "ok" and by_num[3]["doi"] == "10.1109/TSE.2025.3564599"
    assert by_num[4]["status"] == "ok"  # page header after the DOI is not glued on


def test_stalled_session_gets_a_wrapup_that_keeps_its_findings(tmp_path, fake_cli):
    configure, calls = fake_cli
    configure(investigator="noop", wrapup="clear_all")
    path = tmp_path / "l.json"
    ledger.create(path, {}, [{"id": "i", "title": "t"}])
    spec = _spec(tmp_path, path)
    spec.wrapup_prompt = lambda state: "收尾"
    outcome = asyncio.run(session.run(spec))
    assert outcome.status == "done"  # the wrap-up closed every item
    assert ledger.load(path)["items"]["i"]["status"] == "cleared"
    assert calls()[-1]["turns"] == str(config.WRAPUP_TURNS)


def test_writer_output_normalization(tmp_path):
    text = ('先调用工具 {"command": "ls"} 然后输出：\n'
            '{"doi": "x", "data_review": "（1）问题位置：Fig. 1。发现：a。\u3000（2）问题位置：Table 2。发现：b", '
            '"image_review": "核查了全部图像", "ref_review": "r", "methodology_review": "m", "reason": "z"}')
    parsed = engine._review_json(text)
    assert parsed["doi"] == "x"
    shot = tmp_path / "s.png"
    shot.write_bytes(b"x")
    evidence = {"data": [{"path": str(shot), "dimension": "data", "issue_index": 2},
                         {"path": str(shot), "dimension": "data", "issue_index": 5}]}
    images = engine._bind_evidence(parsed, evidence)
    assert "\n\n（2）问题位置" in parsed["data_review"]
    assert [i["issue_index"] for i in images] == [2]


def test_writer_gaps_are_restated_for_the_writer():
    message = engine._writer_gap("Evidence needs a valid issue_index matching a numbered issue in dimension methodology")
    assert "methodology" in message and "（编号）问题位置" in message and "issue_index" not in message


def test_regrade_redoes_calibration_and_writing_only(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    configure(investigator={"issue": {"high::data::0": "substantive"}},
              challenger={"issue": {"high::data::0": "substantive"}},
              calibrator={"issue": {"high::data::0": "substantive"}})
    paper = _make_paper(tmp_path)
    first = _run(paper, tmp_path, monkeypatch)
    assert first["result"] == "高风险"
    before = len(calls())
    configure(calibrator={"issue": {"high::data::0": "minor"}})
    monkeypatch.setattr(config, "REGRADE", True)
    monkeypatch.setattr(engine, "rules_version", lambda: "changed")
    monkeypatch.setattr(engine, "calibration_version", lambda: "changed")
    second = _run(paper, tmp_path, monkeypatch, resume=True)
    roles = [c["actor"].split(":")[0] for c in calls()[before:]]
    assert set(roles) == {"calibrator", "writer"}
    assert second["result"] == "低风险"


def test_volume_pdf_is_scoped_to_the_chapter(tmp_path):
    import fitz
    from review_v3 import dossier
    pdf = tmp_path / "input" / "volume.pdf"
    pdf.parent.mkdir()
    doc = fitz.open()
    for number in range(1, 71):
        page = doc.new_page()
        page.insert_text((72, 72), f"Fig. {number % 7 + 1}. caption on page {number}")
    # front matter, a part title, then three chapters
    doc.set_toc([[1, "Preface", 1], [1, "Part A", 3], [1, "First Paper", 4], [1, "Second Paper", 20],
                 [2, "1 Introduction", 21], [1, "Third Paper", 40], [1, "Author Index", 70]])
    doc.save(str(pdf))
    scope = dossier.chapter_scope(pdf, "10.1007/978-981-96-0000-0/2")
    assert (scope["title"], scope["first"], scope["last"]) == ("Second Paper", 20, 39)
    assert dossier.chapter_scope(pdf, "10.1109/abc.2020.1") is None
    index = dossier.build(tmp_path / "work", "10.1007/978-981-96-0000-0/2", str(pdf.parent), "",
                          {"paper": {"filepath": str(pdf), "title": "Volume Title"}})
    assert index["paper"]["title"] == "Second Paper"
    assert {c["page"] for c in index["captions"]} <= set(range(20, 40))
    assert any("第 20–39 页" in w for w in index["warnings"])


def test_core_claims_check_is_a_required_methodology_shard():
    shards = checklist.build_shards({}, {"main_figure_numbers": [], "captions": [], "data_files": []})
    claims = next(s for s in shards if s["kind"] == "claims")
    assert claims["dimension"] == "methodology" and claims["items"][0]["id"] == "claims::core"
    assert not engine._required_finding("claims::core")


def test_ledger_keeps_conclusions_when_attachments_are_malformed(tmp_path):
    run = tmp_path / "run"
    (run / "ledgers").mkdir(parents=True)
    (run / "dossier" / "renders").mkdir(parents=True)
    (run / "dossier" / "renders" / "p2.png").write_bytes(b"png")
    path = run / "ledgers" / "inv_c.json"
    ledger.create(path, {"shard": "c"}, [{"id": "fig::1", "title": "Fig. 1（第2页）：图注核对"},
                                         {"id": "fig::2", "title": "Fig. 2（第3页）：图注核对"}])
    warnings = []
    applied, errors = ledger.update(path, [
        {"id": "fig::1", "status": "issue", "severity": "minor", "issue_type": "图注与正文不一致",
         "finding": "图注与正文对象不同", "why": "影响理解", "basis": "第1、2页对照",
         "evidence": ["p2.png", "missing.png"]},
        {"id": "fig::2", "status": "cleared", "basis": "曲线与表 2 逐点一致，已核对", "evidence": {"file": "p2.png"}},
    ], "t", warnings)
    assert (applied, errors) == (2, [])
    items = ledger.load(path)["items"]
    assert items["fig::1"]["location"] == "Fig. 1（第2页）"
    assert [Path(e["path"]).name for e in items["fig::1"]["evidence"]] == ["p2.png"]
    assert items["fig::2"]["evidence"][0]["path"].endswith("p2.png")
    assert warnings and "missing.png" in warnings[0]


def test_duplicate_issues_from_two_shards_are_reported_once():
    base = {"status": "issue", "dimension": "data", "issue_type": "结果块重复", "why": "w", "basis": "b"}
    finals = [
        {**base, "id": "new:a", "severity": "substantive", "location": "第7页 Table 3 的 MI 区块",
         "finding": "两行均为 −0.88（−20.26至18.50）、P=0.93，固定效应 14.95（8.37至21.53）", "evidence": [{"path": "a.png"}]},
        {**base, "id": "new:b", "severity": "substantive", "location": "正文第7页 Table 3",
         "finding": "MI 两个模型块均印出 −0.88（−20.26至18.50）与 14.95（8.37至21.53）", "evidence": [{"path": "b.png"}]},
        {**base, "id": "fig::5", "severity": "minor", "location": "第9页 Figure 5",
         "finding": "界值 0.89、0.92、0.95 与图注 0.89 and 0.95"},
        {"id": "x", "status": "cleared", "basis": "ok"},
    ]
    merged = engine._dedupe_issues(finals)
    issues = [r for r in merged if r["status"] == "issue"]
    assert [r["id"] for r in issues] == ["new:a", "fig::5"]
    assert [e["path"] for e in issues[0]["evidence"]] == ["a.png", "b.png"]
    assert any(r["id"] == "x" for r in merged)


def test_guard_keeps_the_input_directory_read_only(monkeypatch):
    from review_v3.tools import guard
    monkeypatch.setenv("PV3_INPUT_DIR", "/data/in/paper")
    monkeypatch.setattr(guard, "ALLOWED_PREFIXES", ["/data/in/paper", "/tmp/"])
    blocked = [
        "python3 -c \"import matplotlib; plt.savefig('/data/in/paper/fig3.png')\"",
        "pdftoppm -png x.pdf > /data/in/paper/p.png",
        "cp /tmp/a.png /data/in/paper/",
        "cd /data/in/paper && python3 - <<'EOF'\nimg.save('crop.png')\nEOF",
    ]
    allowed = [
        "pdftotext -layout /data/in/paper/main.pdf - | head",
        "python3 -c \"import fitz; d=fitz.open('/data/in/paper/main.pdf'); print(d.page_count)\"",
        "cp /data/in/paper/main.pdf /tmp/x.pdf",
    ]
    assert all(guard.check("Bash", {"command": c}) for c in blocked)
    assert not any(guard.check("Bash", {"command": c}) for c in allowed)


def test_input_identity_ignores_files_written_during_review(tmp_path):
    report = tmp_path / "report.json"
    report.write_text("{}")
    paper = tmp_path / "in"
    paper.mkdir()
    (paper / "main.pdf").write_bytes(b"%PDF")
    old = report.stat().st_mtime - 3600
    os.utime(paper / "main.pdf", (old, old))
    before = engine._identity("10.1/x", str(report), str(paper))
    (paper / "arb_fig3.png").write_bytes(b"png")
    later = report.stat().st_mtime + 600
    os.utime(paper / "arb_fig3.png", (later, later))
    assert engine._identity("10.1/x", str(report), str(paper)) == before
    (paper / "extra.xlsx").write_bytes(b"data")
    assert engine._identity("10.1/x", str(report), str(paper)) != before


def test_calibration_severity_follows_the_category(tmp_path):
    from review_v3 import categories
    path = tmp_path / "ledgers" / "cal_all.json"
    path.parent.mkdir()
    first = {"status": "issue", "severity": "substantive", "location": "Table 2", "issue_type": "单格不闭合",
             "finding": "合计 8,524 与分项 8,516 差 8", "why": "主表", "basis": "逐行相加"}
    ledger.create(path, {"shard": "calibrate", "require_category": True},
                  [{"id": "a", "title": "A", "first": first}, {"id": "b", "title": "B", "first": first}])
    applied, errors = ledger.update(path, [{"id": "a", "status": "issue", "severity": "substantive"}], "cal")
    assert applied == 0 and "category" in errors[0]
    applied, errors = ledger.update(path, [
        {"id": "a", "status": "issue", "category": "m1", "severity": "substantive", "why": "孤立小差异，不改变结论"},
        {"id": "b", "status": "issue", "category": "S3", "why": "结论方向被表 3 否定"}], "cal")
    items = ledger.load(path)["items"]
    assert (applied, errors) == (2, [])
    assert (items["a"]["category"], items["a"]["severity"]) == ("M1", "minor")
    assert items["b"]["severity"] == "substantive" and items["b"]["finding"] == first["finding"]
    assert all(code in categories.table() for code in categories.CATEGORIES)


def test_report_only_change_rewrites_without_recalibrating(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    configure(investigator={"issue": {"high::data::0": "substantive"}},
              challenger={"issue": {"high::data::0": "substantive"}},
              calibrator={"issue": {"high::data::0": "substantive"}})
    paper = _make_paper(tmp_path)
    assert _run(paper, tmp_path, monkeypatch)["result"] == "高风险"
    before = len(calls())
    monkeypatch.setattr(config, "REGRADE", True)
    monkeypatch.setattr(engine, "report_version", lambda: "new-format")
    again = _run(paper, tmp_path, monkeypatch, resume=True)
    assert {c["actor"].split(":")[0] for c in calls()[before:]} == {"writer"}
    assert again["result"] == "高风险" and again.get("_report_json")


def test_report_wording_check_flags_process_terms():
    assert engine._wording_gaps({"reason": "该问题按实质问题处理，属 S2 类"})
    assert not engine._wording_gaps({"reason": "Table 3 两个模型的系数逐位相同，需作者说明。最终判定：建议高风险"})


def test_guard_reads_heredoc_bodies_as_data_and_keeps_calibration_offline(monkeypatch):
    from review_v3.tools import guard
    ledger_write = "cd /tmp && pv3 ledger update <<'EOF'\n[{\"id\": \"x\", \"finding\": \"Fig.1 / Fig.2 find / ls /\"}]\nEOF"
    assert guard.check("Bash", {"command": ledger_write}) is None
    monkeypatch.setenv("PV3_ACTOR", "calibrator")
    assert guard.check("Bash", {"command": "timeout 60 curl -s https://api.crossref.org/works/10.1/x"})
    assert guard.check("Bash", {"command": "pv3 crossref --doi 10.1/x"})
    monkeypatch.setenv("PV3_ACTOR", "investigator:ref")
    assert guard.check("Bash", {"command": "pv3 crossref --doi 10.1/x"}) is None


def test_unfinished_must_check_items_are_completed_instead_of_erroring(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    # Every regular stage stalls; only the follow-up batch reaches a conclusion.
    configure(investigator="noop", challenger="noop", arbiter="noop", **{"investigator:fin1_1": "clear_all"})
    result = _run(_make_paper(tmp_path), tmp_path, monkeypatch)
    assert result["trigger"] != "review_error" and result["result"] == "低风险"
    assert any(c["actor"] == "investigator:fin1_1" for c in calls())


def test_coverage_gap_is_answered_from_verified_conclusions_only():
    evidence = {"high_findings": [{"dim": "data", "test": "passage_offset_pattern", "location": "s.xlsx / Table_5 (cols 4-6)"},
                                  {"dim": "data", "test": "terminal_digit", "location": "s.xlsx / Sheet2"}]}
    finals = {"group::a": {"id": "group::a", "status": "cleared", "members": ["high::data::0"],
                           "basis": "逐行复现告警行对，为不同研究的相同计数。"},
              "high::data::1": {"id": "high::data::1", "status": "unresolved", "note": "未完成"}}
    gaps = ["未对HIGH发现给出结论: data/passage_offset_pattern (共1项)",
            "未对HIGH发现给出结论: data/terminal_digit (共1项)"]
    patched, rest = engine._coverage_patch({"data_review": "本节结论：未发现风险。"}, gaps, finals, evidence)
    assert "跨行数值偏移（Table_5 (cols 4-6)" in patched["data_review"] and "已核对" in patched["data_review"]
    # An unverified hit is disclosed as not checked, never as checked.
    assert rest == [] and "末位数字分布（Sheet2，共1项）补查后仍未能完成逐项核对" in patched["data_review"]


def test_existing_ledger_gains_items_found_later(tmp_path):
    path = tmp_path / "l.json"
    ledger.create(path, {"shard": "x"}, [{"id": "a", "title": "A"}])
    ledger.update(path, [{"id": "a", "status": "cleared", "basis": "核对第2页表1，数值一致"}], "t")
    again = ledger.create(path, {"shard": "x"}, [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}])
    items = ledger.load(path)["items"]
    assert again["added"] == 1 and items["a"]["status"] == "cleared" and items["b"]["status"] == "open"


def test_report_is_assembled_from_facts_when_the_writer_keeps_failing(tmp_path, fake_cli, monkeypatch):
    configure, _ = fake_cli
    configure(investigator={"issue": {"high::data::0": "substantive"}},
              challenger={"issue": {"high::data::0": "substantive"}},
              calibrator={"issue": {"high::data::0": "substantive"}}, writer="broken")
    result = _run(_make_paper(tmp_path), tmp_path, monkeypatch)
    assert result["trigger"] != "review_error" and result["result"] == "高风险"
    assert result["_report_source"] == "template"
    assert "（1）问题位置：" in result["data_review"] and "最终判定：建议高风险" in result["reason"]


def test_late_phase_still_gets_its_floor(tmp_path, monkeypatch):
    import time as _time
    review = engine.Review("10.1/x", "", str(tmp_path), tmp_path, None, _time.monotonic() - 3600)
    # The arbitration box (42 min after the start) passed long ago.
    deadline = review._phase_deadline("arbitrate")
    assert deadline >= _time.monotonic() + config.PHASE_FLOOR_SECONDS["arbitrate"] - 1
    assert review._phase_deadline("investigate") < _time.monotonic()  # no floor for the first phase
