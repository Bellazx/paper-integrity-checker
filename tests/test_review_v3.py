"""Offline tests for review engine v3 (fake CLI, no model or network)."""
import asyncio
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_session_id_left_by_an_interrupted_run_is_replaced_at_once(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    configure(investigator="clear_all", session_in_use_first=True)
    monkeypatch.setattr(config, "RETRY_BACKOFF_SECONDS", 3600)  # a backoff would hang the test
    monkeypatch.setattr(config, "TRANSPORT_RETRIES", 0)
    path = tmp_path / "l.json"
    ledger.create(path, {}, [{"id": "i", "title": "t"}])
    outcome = asyncio.run(session.run(_spec(tmp_path, path)))
    assert outcome.status == "done"
    first, second = calls()[0]["session"], calls()[1]["session"]
    assert len(calls()) == 2 and first != second


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
    assert result["result"] == "低风险" and "另需作者更正" in result["_coverage_notes"]
    assert result["data_review"] == "本模块复核结论：建议低风险。"  # the PDF lists risk issues only
    assert "问题位置" not in result["data_review"]


def test_engine_unfinished_checks_are_disclosed_not_blocking(tmp_path, fake_cli, monkeypatch):
    configure, _ = fake_cli
    configure(investigator="noop", challenger="noop", arbiter="noop")
    paper = _make_paper(tmp_path)
    result = _run(paper, tmp_path, monkeypatch)
    # Still unverified after the completion pass: the verdict rests on what was
    # verified and the report says which screening hits were not checked.
    assert result["trigger"] != "review_error" and result["result"] == "低风险"
    assert "未能完成" in result["_coverage_notes"] and "未能完成" not in result["data_review"]


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
    assert result["result"] == "低风险" and "另需作者更正" in result["_coverage_notes"]
    assert result["data_review"] == "本模块复核结论：建议低风险。"  # the PDF lists risk issues only
    assert any(c["actor"] == "calibrator" for c in calls())
    finals = json.loads(Path(result["_review_workdir"], "finals.json").read_text())
    assert finals["finals"]["high::data::0"]["calibrated_from"]["severity"] == "substantive"


# ------------------------------------------------------------------ entry points

def test_api_entry_dispatches_to_v3_only_when_enabled(monkeypatch):
    import api.services.review as review_module
    seen = {}

    async def fake_run(doi, report, input_dir, output_dir, resume=False, deadline=None, shared_api_budget=False):
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
    configure(calibrator={"issue": {"high::data::0": "minor"}}, judge={"issue": {"high::data::0": "minor"}})
    monkeypatch.setattr(config, "REGRADE", True)
    monkeypatch.setattr(engine, "rules_version", lambda: "changed")
    monkeypatch.setattr(engine, "calibration_version", lambda: "changed")
    second = _run(paper, tmp_path, monkeypatch, resume=True)
    roles = [c["actor"].split(":")[0] for c in calls()[before:]]
    # A downgraded issue is still reviewed by the judge, which here upholds the downgrade.
    assert set(roles) == {"calibrator", "judge", "writer"}
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
    configure(investigator="noop", challenger="noop", arbiter="noop", **{"investigator:fin1_data-1": "clear_all"})
    result = _run(_make_paper(tmp_path), tmp_path, monkeypatch)
    assert result["trigger"] != "review_error" and result["result"] == "低风险"
    assert any(c["actor"] == "investigator:fin1_data-1" for c in calls())


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


def test_screening_hit_merged_into_source_audit_item_is_answered_in_plain_words():
    # The evidence builder drops a screening hit that repeats a source-data
    # audit finding; the audit item's conclusion answers the gate.
    evidence = {"high_findings": [{"dim": "data", "test": "same_sheet_channel_duplicate",
                                   "location": "s.zip / panel_i_df / 测量通道 / E vs F"}]}
    finals = {"source_data_audit::same_sheet_channel_duplicate::1": {
        "id": "source_data_audit::same_sheet_channel_duplicate::1", "status": "issue",
        "issue_type": "两通道误差列逐值相同（规范问题）"}}
    gaps = ["未对HIGH发现给出结论: data/same_sheet_channel_duplicate (共1项)"]
    patched, rest = engine._coverage_patch({"data_review": "本节结论：未发现风险。"}, gaps, finals, evidence)
    assert rest == [] and "不同通道数值重复（panel_i_df）存在问题：两通道误差列逐值相同，" in patched["data_review"]
    assert not engine._wording_gaps(patched)
    assert engine._plain("故按次要问题（规范问题）定级") == "故按次要问题判断"


def test_screening_class_without_any_record_is_disclosed_as_not_checked():
    evidence = {"high_findings": [{"dim": "data", "test": "verified_manuscript_content_conflict", "location": "PDF第7页"}]}
    gaps = ["未对HIGH发现给出结论: data/verified_manuscript_content_conflict (共1项)"]
    patched, rest = engine._coverage_patch({"data_review": "本节结论：未发现风险。"}, gaps, {}, evidence)
    assert rest == [] and "正文内容不一致（PDF第7页，共1项）补查后仍未能完成逐项核对" in patched["data_review"]


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


def test_substantive_label_contradicting_its_own_impact_statement_is_minor():
    base = {"status": "issue", "severity": "substantive"}
    assert engine._admits_minor({**base, "category": "S4", "why": "属正文派生量标签错位，不支持判为实质问题"})
    assert engine._admits_minor({**base, "category": "S3", "why": "表述过宽，结论方向未被推翻"})
    # Data authenticity and genuine carrier conflicts keep their label.
    assert not engine._admits_minor({**base, "category": "S1", "why": "两模型结果块逐位相同；论文结论方向不变"})
    assert not engine._admits_minor({**base, "category": "S2", "why": "MI 结果块复用，不是规范问题；结论方向不变"})


def test_correction_points_are_not_read_as_risk_issues():
    from modules.review_report_layout import _ISSUE_START
    text = ("本节结论：有风险。\n（1）问题位置：Table 3\n问题类型：x\n发现：y\n为什么重要：z\n核查依据：w\n"
            "另需作者更正：\n① 位置：第2页 Fig. 1；问题：图注写 Fig. 3；请改为 Fig. 2。\n② 位置：文献 [4]；问题：作者缺失。")
    assert [m.group(1) for m in _ISSUE_START.finditer(text)] == ["1"]


def test_investigation_stops_before_the_paper_deadline(tmp_path):
    import time as _time
    review = engine.Review("10.1/x", "", str(tmp_path), tmp_path, _time.monotonic() + 3600, _time.monotonic())
    assert review._hard_deadline("investigate") == review.deadline - config.FINISH_RESERVE_SECONDS
    assert review._hard_deadline("calibrate") == review.deadline - config.GRADING_RESERVE_SECONDS


def test_final_review_by_judge_can_overturn_a_substantive_issue(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    configure(investigator={"issue": {"high::data::0": "substantive"}},
              challenger={"issue": {"high::data::0": "substantive"}},
              calibrator={"issue": {"high::data::0": "substantive"}},
              judge={"issue": {"high::data::0": "minor"}})
    result = _run(_make_paper(tmp_path), tmp_path, monkeypatch)
    assert result["result"] == "低风险" and "另需作者更正" in result["_coverage_notes"]
    assert result["data_review"] == "本模块复核结论：建议低风险。"  # the PDF lists risk issues only
    judge_calls = [c for c in calls() if c["actor"] == "judge"]
    assert judge_calls and judge_calls[0]["model"] == config.JUDGE_MODEL


def test_final_review_upholding_keeps_high_risk(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    configure(investigator={"issue": {"high::data::0": "substantive"}},
              challenger={"issue": {"high::data::0": "substantive"}},
              calibrator={"issue": {"high::data::0": "substantive"}})
    result = _run(_make_paper(tmp_path), tmp_path, monkeypatch)
    assert result["result"] == "高风险"
    models = {c["actor"].split(":")[0]: c["model"] for c in calls()}
    assert models["judge"] == config.JUDGE_MODEL and models["investigator"] == config.MODEL


def test_paper_deadline_still_publishes_a_report(tmp_path, fake_cli, monkeypatch):
    configure, _ = fake_cli
    # The investigator records one issue and then hangs past the paper deadline.
    configure(investigator={"issue": {"high::data::0": "substantive"}}, delay=0)
    paper = _make_paper(tmp_path)
    monkeypatch.setattr(config, "PAPER_TIMEOUT_SECONDS", 4)
    original = engine._review_phases

    async def slow(review, *args, **kwargs):
        await review.prepare(args[2])
        ledger.update(review.ledger_path("inv", review.shards[0]["id"]),
                      [{"id": review.shards[0]["items"][0]["id"], "status": "cleared",
                        "basis": "打开源数据 Sheet1 逐值核对，属共享横轴列，正常"}], "investigator")
        await asyncio.sleep(30)
        return await original(review, *args, **kwargs)
    monkeypatch.setattr(engine, "_review_phases", slow)
    result = _run(paper, tmp_path, monkeypatch)
    assert result["trigger"] != "review_error" and result["_report_source"] == "deadline"
    assert result["result"] in ("高风险", "低风险") and "最终判定" in result["reason"]


def test_judge_also_reviews_issues_the_calibrator_downgraded(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    configure(investigator={"issue": {"high::data::0": "substantive"}},
              challenger={"issue": {"high::data::0": "substantive"}},
              calibrator={"issue": {"high::data::0": "minor"}},
              judge={"issue": {"high::data::0": "substantive"}})
    result = _run(_make_paper(tmp_path), tmp_path, monkeypatch)
    assert any(c["actor"] == "judge" for c in calls())
    assert result["result"] == "高风险"


def test_large_prompts_go_through_stdin(tmp_path, fake_cli):
    configure, calls = fake_cli
    configure()
    spec = session.SessionSpec(name="big", workdir=tmp_path, prompt="核查" + "填" * 200_000, actor="writer",
                               tools=False, max_chunks=1)
    asyncio.run(session.run(spec))
    # The CLI process started and received the prompt (an over-long argv
    # would fail before the process exists).
    assert any(c["actor"] == "writer" and c["prompt_chars"] > 200_000 for c in calls())


def test_every_gated_screening_type_has_a_label_with_its_keyword():
    import inspect
    from modules import review_coverage
    source = inspect.getsource(review_coverage)
    block = source[source.index("_type_cues = {"):source.index("for (dim, test), items in groups.items():")]
    cues = {}
    exec(block.strip(), {}, cues)
    for test, words in cues["_type_cues"].items():
        label = engine.TEST_LABELS.get(test, "")
        assert any(w.lower() in label.lower() for w in words), (test, label)


def test_duplicate_column_pairs_are_grouped_by_sheet():
    pairs = [{"file": "S1.xlsx", "sheet": f"Fig{i % 3}", "col_a_header": f"a{i}", "col_b_header": f"b{i}",
              "cross_group": i == 7} for i in range(300)]
    evidence = {"deterministic_findings": {"duplicate_column_pairs": pairs},
                "coverage_manifest": {"must_address": [f"dupcol::S1.xlsx::Fig{i % 3}::{i}" for i in range(300)]}}
    shards = checklist.build_shards(evidence, {"main_figure_numbers": [], "captions": [], "data_files": ["S1.xlsx"]})
    groups = [i for s in shards for i in s["items"] if i["id"].startswith("group::data::duplicate_column_pairs")]
    assert len(groups) == 3 and sum(len(g["members"]) for g in groups) == 300
    assert any(g["strong"] for g in groups) and all("重复列对" in g["title"] and "S1.xlsx" in g["title"] for g in groups)
    assert engine._reader_label(groups[0]["title"]).startswith("重复列（S1 / Fig")


def test_decimal_mismatch_columns_are_grouped_by_file():
    columns = [{"header": f"col_{i}", "precision_spread": [2, 15], "locations": ["S4.xlsx::Figure1", "S4.xlsx::Figure2"]}
               for i in range(500)]
    evidence = {"deterministic_findings": {"decimal_precision_mismatch": columns},
                "coverage_manifest": {"must_address": [f"decmismatch::col_{i}" for i in range(500)]}}
    shards = checklist.build_shards(evidence, {"main_figure_numbers": [], "captions": [], "data_files": ["S4.xlsx"]})
    items = [i for s in shards for i in s["items"] if "decmismatch" in i["id"] or "decimal" in i["id"]]
    assert len(items) == 1 and len(items[0]["members"]) == 500
    assert engine._reader_label(items[0]["title"]) == "小数精度不一致（S4）"
    finals = {items[0]["id"]: {"id": items[0]["id"], "status": "cleared", "members": items[0]["members"],
                               "basis": "各列为仪器导出的原始精度与计算列混排，属正常。"}}
    patched, rest = engine._coverage_patch({"data_review": "本节结论：未发现风险。"},
                                           ["未说明小数精度不一致类别（共500项）"], finals, {})
    assert rest == [] and "小数精度" in patched["data_review"] and "已核对" in patched["data_review"]


def test_completion_follow_up_is_capped_per_paper(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    configure(investigator="noop", challenger="noop", arbiter="noop")
    monkeypatch.setattr(config, "COMPLETE_BATCH", 1)
    monkeypatch.setattr(config, "COMPLETE_MAX_BATCHES", 1)  # two follow-up batches per round without the cap
    paper = _make_paper(tmp_path)
    evidence = paper["evidence_obj"]
    for column in ("a", "b", "c", "d"):
        evidence["high_findings"].append({"dim": "data", "test": "value_recycling", "location": f"data.xlsx / Sheet1 / {column}"})
    evidence["coverage_manifest"]["must_address"] = [f"high::data::{i}" for i in range(5)]
    Path(paper["evidence"]).write_text(json.dumps(evidence))
    result = _run(paper, tmp_path, monkeypatch)
    rounds = {}
    for call in calls():
        if call["actor"].startswith("investigator:fin"):
            rounds.setdefault(call["actor"].split(":")[1].split("_")[0], set()).add(call["actor"])
    # Each shard may follow up independently; the shared semaphore caps
    # concurrent sessions. The newly required visual pass gets its own batch.
    assert rounds and all(len(names) == 2 for names in rounds.values())
    assert all(any('image_scan' in name for name in names) for names in rounds.values())
    assert result["trigger"] != "review_error"


def test_long_fact_lists_are_summarised():
    entries = [{"条目": f"重复列对 {i}"} for i in range(config.FACTS_LIST_LIMIT + 5)]
    capped = engine._capped(entries, "均已核对，未发现问题")
    assert len(capped) == config.FACTS_LIST_LIMIT + 1 and capped[-1]["条目"] == "另有 5 项，均已核对，未发现问题"


def test_issue_without_screenshot_gets_the_cited_figure_cropped(tmp_path):
    import fitz
    from review_v3 import autocrop
    pdf = fitz.open()
    contents = pdf.new_page()
    for i in range(1, 6):
        contents.insert_text((50, 60 + 30 * i), f"Supplementary Figure {i}. A long title that describes this figure in some detail.")
    page = pdf.new_page()
    page.draw_rect(fitz.Rect(60, 60, 540, 420), color=(0, 0, 1))
    page.insert_textbox(fitz.Rect(50, 440, 560, 520), "Fig. 2 | Measured dissolution curves for both catalysts under "
                        "the potential program, with the baseline segment shown in panel h.", fontsize=9)
    path = tmp_path / "main.pdf"
    pdf.save(str(path))
    dossier = {"docs": [{"id": "main", "path": str(path), "pages": 2}]}
    shots = autocrop.crop_for({"location": "正文第2页 Fig. 2h", "finding": "x"}, dossier, tmp_path)
    assert len(shots) == 1 and ledger.is_crop(shots[0]["path"]) and Path(shots[0]["path"]).is_file()
    assert shots[0]["caption"] == "正文第2页 Fig. 2（原文截图）"
    # A contents page lists captions but shows no figure.
    assert autocrop.crop_for({"location": "Supplementary Fig. 3", "finding": "x"},
                             {"docs": [{"id": "s1", "path": str(path), "pages": 2}]}, tmp_path) == []


def test_source_data_issue_gets_its_workbook_cells_as_evidence(tmp_path):
    import openpyxl
    from review_v3 import autocrop
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Fig5h"
    for i in range(40):
        ws.append([i, i * 0.1, i * 0.2])
    wb.save(tmp_path / "source.xlsx")
    cells = autocrop.cells_for({"location": "随文源数据 source.xlsx 工作表 Fig5h 第 20–27 行 B 列", "finding": ""},
                               str(tmp_path))
    assert cells == [{"file": "source.xlsx", "sheet": "Fig5h", "range": "B19:B30",
                      "caption": "随文源数据 Fig5h 表 B19:B30（单元格原值）"}]
    assert ledger.is_excel(cells[0]) and ledger.is_evidence(cells[0])


def _ruled_table_pdf(path, rows=4):
    """A page with a captioned table drawn with rules, then a body paragraph."""
    import fitz
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_textbox(fitz.Rect(90, 72, 560, 106), "Supplementary Table 2 Comparison of anthropogenic black carbon "
                        "direct radiative forcing and effective radiative forcing", fontsize=10)
    y = 110
    page.draw_line((90, y), (490, y))
    body = "\n".join(f"Study {i}    2015    1.{i}1    1.83" for i in range(rows))
    page.insert_textbox(fitz.Rect(90, y + 2, 490, y + 2 + 16 * rows), body, fontsize=10)
    y += 4 + 16 * rows
    page.draw_line((90, y), (490, y))
    page.insert_textbox(fitz.Rect(90, y + 60, 500, y + 200), "Body text " * 40, fontsize=10)
    pdf.save(str(path))
    return y


def test_table_screenshot_ends_at_the_table(tmp_path):
    # A short table used to be cut at 45% of the page height, half of it blank.
    import fitz
    from review_v3 import autocrop
    bottom_rule = _ruled_table_pdf(tmp_path / "s.pdf")
    page = fitz.open(str(tmp_path / "s.pdf"))[0]
    rect = autocrop._table_rect(page, autocrop._caption_rect(page, "table", "2"))
    assert bottom_rule <= rect.y1 <= bottom_rule + 10


def test_long_table_body_closed_by_a_rule_is_kept(tmp_path):
    # Rows set as one long text block, with a wide gap between header and
    # bottom rules, are still the table, not a body paragraph.
    import fitz
    from review_v3 import autocrop
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_textbox(fitz.Rect(36, 357, 580, 395), "Supplementary Table 4. Solar-blind ultraviolet "
                        "photodetectors based on Schottky junction of inorganic materials.", fontsize=10)
    page.draw_line((36, 399), (560, 399))
    page.insert_textbox(fitz.Rect(89, 401, 558, 430), "Materials  Wavelength  P  R  D*  Reference", fontsize=10)
    page.draw_line((36, 431), (560, 431))
    page.insert_textbox(fitz.Rect(67, 434, 536, 664), "\n".join(f"Ga2O3 MSM {i}   254   87   1.70   230   {50 + i}"
                                                                  for i in range(15)), fontsize=10)
    page.draw_line((36, 666), (560, 666))
    rect = autocrop._table_rect(page, autocrop._caption_rect(page, "table", "4"))
    assert 664 <= rect.y1 <= 680


def test_workbook_cells_are_found_by_the_quoted_values(tmp_path):
    # Two figure blocks side by side in one sheet: the range is the block that
    # holds the quoted values, with its title, header row and row labels.
    import openpyxl
    from review_v3 import autocrop
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Fig.1"
    years = [2015, 2020, 2030, 2040, 2050, 2060]
    ws.append([])
    ws.append(["Fig.1c", "PKU", *years, "Fig.1d", "PKU-BC", *years])
    for k, name in enumerate(("SSP1", "SSP2", "SSP3")):
        ws.append([None, name, *(50.0 + k + i for i in range(6)), None, name, 1.7005281876211287, *(0.5 + i / 10 for i in range(5))])
    ws.append([None, "CMIP6", *years, None, "CMIP6", *years])
    for k, name in enumerate(("SSP1", "SSP2", "SSP3")):
        ws.append([None, name, *(26.0 + k + i for i in range(6)), None, name, 2.1408835006086777, *(1.5 + i / 10 for i in range(5))])
    other = wb.create_sheet("Fig.10")
    other.append([1.7005281876211287])
    wb.save(tmp_path / "source.xlsx")
    record = {"location": "正文第4页",
              "finding": "源数据 Fig.1d（Fig.1 sheet 第3-9行）为 1.7005281876211287 与 2.1408835006086777"}
    cells = autocrop.cells_for(record, str(tmp_path))
    assert [(c["sheet"], c["range"]) for c in cells] == [("Fig.1", "I2:P9")]


def test_conflict_between_paper_and_source_data_shows_both_sides(tmp_path, monkeypatch):
    # The issue already has the printed table; the source-data cells it is set
    # against are added, and a capture session is asked only for that side.
    import openpyxl
    from review_v3 import session as session_mod
    _ruled_table_pdf(tmp_path / "supp.pdf")
    inputs = tmp_path / "input"
    inputs.mkdir()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Fig.1"
    ws.append(["Fig.1d", "PKU-BC", 2015])
    ws.append([None, "SSP1", 1.7005281876211287])
    wb.save(inputs / "source.xlsx")
    crop = tmp_path / "s1_p001_c20_69_575_200_d150.png"
    crop.write_bytes(b"png")
    record = {"id": "high::data::8", "status": "issue", "severity": "substantive", "dimension": "methodology",
              "location": "Supplementary Table 2（第1页）", "title": "ERF",
              "finding": "Table 2 为 1.83，而源数据 Fig.1 sheet 第2行为 1.7005281876211287",
              "evidence": [{"path": str(crop), "caption": "补充材料第1页 Table S2（原文截图）"}]}
    assert engine._missing_carriers(record, str(inputs)) == {"workbook"}
    prompts_seen = []

    async def no_session(spec):
        prompts_seen.append(spec.prompt)

    monkeypatch.setattr(session_mod, "run", no_session)
    review = engine.Review("10.1/x", "", str(inputs), tmp_path / "run", None)
    review.dossier = {"docs": [{"id": "s1", "name": "supp.pdf", "path": str(tmp_path / "supp.pdf"), "pages": 1}]}
    monkeypatch.setattr(engine.Review, "_spec", lambda self, name, prompt, *a: SimpleNamespace(prompt=prompt))
    out = asyncio.run(review.capture({record["id"]: record}))
    evidence = out[record["id"]]["evidence"]
    assert evidence[0]["path"] == str(crop)
    assert [(e.get("sheet"), e.get("range")) for e in evidence[1:]] == [("Fig.1", "A1:C2")]
    assert prompts_seen == []  # the explicit cell range was cropped before any model call
    assert engine._missing_carriers(out[record["id"]], str(inputs)) == set()


def test_source_data_cells_render_in_the_section_of_their_issue():
    parsed = {"methodology_review": "本节结论：有风险。\n（1）问题位置：Table S2 与源数据。问题：互斥。影响：结论改变。",
              "data_review": "本节结论：未发现风险。", "image_review": "", "ref_review": ""}
    evidence = {"methodology": [{"file": "s.xlsx", "sheet": "Fig.1", "range": "I11:P18", "caption": "c",
                                 "dimension": "methodology", "issue_index": 1}]}
    images = engine._bind_evidence(parsed, evidence)
    assert images == []
    assert parsed["excel_evidence_regions"][0]["dimension"] == "methodology"


def test_tool_waits_split_at_the_guard_hook(tmp_path, monkeypatch):
    # A tool call that waits 25 s before the guard runs is reported as such:
    # the wait is in the CLI or the machine, not in the command.
    from datetime import datetime, timezone
    monkeypatch.setattr(session, "TRANSCRIPTS", tmp_path / "projects")
    folder = tmp_path / "projects" / "x"
    folder.mkdir(parents=True)

    def stamp(seconds):
        return datetime.fromtimestamp(1000 + seconds, timezone.utc).isoformat().replace("+00:00", "Z")

    events = []
    for i, (use, hook, result) in enumerate([(0, 25.2, 25.4), (30, 55.3, 55.5), (60, 60.1, 60.2)]):
        events.append({"timestamp": stamp(use), "message": {"content": [{"type": "tool_use", "id": f"t{i}"}]}})
        events.append({"timestamp": stamp(result), "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}"}]}})
    (folder / "sid.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    workdir = tmp_path / "run"
    workdir.mkdir()
    (workdir / "hook_times.log").write_text(
        "".join(f"{1000 + h:.3f}\t{name}\tBash\n" for h, name in [(25.2, "inv_a.json"), (55.3, "inv_a.json"),
                                                                    (60.1, "inv_a.json"), (25.0, "other.json")]))
    waits = session.tool_waits("sid", 1000, workdir, workdir / "inv_a.json")
    assert waits == {"tool_calls": 3, "tool_wait_s": 25.4, "before_hook_s": 25.2}


def test_guard_records_when_it_starts(tmp_path):
    import subprocess as sp
    env = {**os.environ, "PV3_WORKDIR": str(tmp_path), "PV3_LEDGER": str(tmp_path / "ledgers" / "inv_a.json")}
    guard = Path(engine.__file__).parent / "tools" / "guard.py"
    sp.run([sys.executable, "-I", str(guard)], input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "pv3 info"}}),
           text=True, env=env, check=True)
    stamp, ledger_name, tool = (tmp_path / "hook_times.log").read_text().strip().split("\t")
    assert float(stamp) > 0 and ledger_name == "inv_a.json" and tool == "Bash"


def test_cli_waiting_for_background_jobs_after_its_turn_limit_is_ended(tmp_path, fake_cli, monkeypatch):
    # At its turn limit the CLI waits for background jobs before exiting; the
    # chunk is over then, so the wait is cut short instead of running to the
    # chunk timeout, and the session goes on as after any turn-limited chunk.
    configure, calls = fake_cli
    configure(hang_after_max_turns=40)
    monkeypatch.setattr(session, "TRANSCRIPTS", tmp_path / "projects")
    monkeypatch.setenv("FAKE_TRANSCRIPTS", str(tmp_path / "projects"))
    monkeypatch.setattr(config, "EXIT_WAIT_SECONDS", 1)
    monkeypatch.setattr(config, "WATCH_POLL_SECONDS", 0.2)
    path = tmp_path / "l.json"
    ledger.create(path, {}, [{"id": "i", "title": "t"}])
    spec = _spec(tmp_path, path)
    spec.max_chunks = 2
    outcome = asyncio.run(session.run(spec))
    assert [m.get("exit_killed") for m in outcome.metrics] == [True, True]
    assert all(m["wall"] < 15 and m["error"] is None and m["subtype"] == "error_max_turns" for m in outcome.metrics)
    assert [c["resume"] for c in calls()] == [False, True]


def test_transcript_watch_reads_only_what_the_current_chunk_wrote(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "TRANSCRIPTS", tmp_path)
    (tmp_path / "p").mkdir()
    transcript = tmp_path / "p" / "sid.jsonl"
    limit = json.dumps({"type": "attachment", "attachment": {"type": "max_turns_reached", "turnCount": 13}})
    transcript.write_text(limit + "\n")  # an earlier chunk of the resumed session
    tail = session._TranscriptTail("sid")
    tail.start()
    assert tail.turn_limit_events() == []
    with open(transcript, "a") as handle:
        handle.write(json.dumps({"type": "user", "message": {"content": "x" * 5000}}) + "\n" + limit[:20])
    assert tail.turn_limit_events() == []  # the half-written line waits for its end
    with open(transcript, "a") as handle:
        handle.write(limit[20:] + "\n")
    assert [e["turnCount"] for e in tail.turn_limit_events()] == [13]


def test_guard_refuses_background_commands(tmp_path):
    env = {"PV3_WORKDIR": str(tmp_path), "PV3_INPUT_DIR": str(tmp_path / "paper"), "PAPER_INTEGRITY_ROOT": str(ROOT)}
    refused = _guard("Bash", {"command": "python3 big.py", "run_in_background": True}, env)
    assert refused.returncode == 2 and "run_in_background" in refused.stderr
    assert _guard("Bash", {"command": "python3 big.py", "run_in_background": False}, env).returncode == 0


def _two_issue_paper(tmp_path):
    paper = _make_paper(tmp_path)
    evidence = paper["evidence_obj"]
    evidence["high_findings"].append({"dim": "data", "test": "decimal_mismatch", "location": "data.xlsx / Sheet1 / treat"})
    evidence["coverage_manifest"] = {"must_address": ["high::data::0", "high::data::1"],
                                     "counts": {"total_must_address": 2}}
    Path(paper["evidence"]).write_text(json.dumps(evidence))
    return paper


def test_calibration_starts_before_the_slowest_shard_and_takes_its_issues_later(tmp_path, fake_cli, monkeypatch):
    # One calibrator per paper: it starts when the first shard has settled and
    # the slow shard's issue is added to the same session (resumed, context kept).
    configure, calls = fake_cli
    both = {"high::data::0": "substantive", "high::data::1": "substantive"}
    monkeypatch.setattr(config, "DATA_SHARD_SIZE", 1)  # the two findings land in two shards
    paper = _two_issue_paper(tmp_path)
    shards = engine.checklist.build_shards(json.loads(Path(paper["evidence"]).read_text()), {})
    slow = next(s["id"] for s in shards if any(i["id"] == "high::data::1" for i in s["items"]))
    configure(investigator={"issue": both}, challenger={"issue": both},
              calibrator={"issue": {"high::data::0": "substantive", "high::data::1": "minor"}},
              delays={f"investigator:{slow}": 4})
    monkeypatch.setattr(config, "CALIBRATE_START_FRACTION", 0.01)
    result = _run(paper, tmp_path, monkeypatch)
    graders = [c for c in calls() if c["actor"] == "calibrator"]
    assert {Path(c["ledger"]).name for c in graders} == {"cal_all.json"}
    assert not graders[0]["resume"] and not graders[0]["added_items"]
    # The slow shard's issue reached a calibrator that had already started.
    assert any(c["resume"] and c["added_items"] for c in graders[1:])
    finals = json.loads(Path(result["_review_workdir"], "finals.json").read_text())["finals"]
    assert finals["high::data::0"]["severity"] == "substantive"
    assert finals["high::data::1"]["severity"] == "minor"
    assert result["result"] == "高风险"


def test_reopened_session_resumes_and_keeps_counting_its_chunks(tmp_path, fake_cli):
    configure, calls = fake_cli
    configure(investigator="clear_all")
    path = tmp_path / "l.json"
    ledger.create(path, {}, [{"id": "a", "title": "t"}])
    first = asyncio.run(session.run(_spec(tmp_path, path)))
    assert first.status == "done"
    ledger.create(path, {}, [{"id": "a", "title": "t"}, {"id": "b", "title": "t"}])
    assert asyncio.run(session.run(_spec(tmp_path, path))).status == "done"  # finished sessions stay finished
    assert len(calls()) == 1
    used = session.reopen("s", tmp_path)
    again = asyncio.run(session.run(_spec(tmp_path, path, max_chunks=used + 2)))
    assert again.status == "done" and len(again.metrics) == 2
    assert [c["resume"] for c in calls()] == [False, True]
    assert ledger.load(path)["items"]["b"]["status"] == "cleared"


def test_slow_tool_calls_turn_on_cli_debug_logging_for_the_next_chunks(tmp_path, fake_cli, monkeypatch):
    configure, calls = fake_cli
    configure(investigator="one_per_call")
    waits = iter([{"tool_calls": 5, "tool_wait_s": 25.7, "before_hook_s": 25.5}, {}, {}])
    monkeypatch.setattr(session, "tool_waits", lambda *args: next(waits))
    path = tmp_path / "l.json"
    ledger.create(path, {}, [{"id": f"i{n}", "title": "t"} for n in range(3)])
    assert asyncio.run(session.run(_spec(tmp_path, path))).status == "done"
    assert [c["debug"] for c in calls()] == [False, True, True]


def test_figure_sentence_ignores_records_without_a_figure_number():
    finals = {"fig::overall": {"id": "fig::overall", "status": "issue", "severity": "minor"},
              "figure::x_0001.png": {"id": "figure::x_0001.png", "status": "cleared"}}
    sentence = engine._figure_sentence(finals, {"main_figures": [{}, {}, {}]})
    assert "overall" not in sentence and "Fig. 1" in sentence and "Fig. 3" in sentence
    unfinished = {"fig::overall": {"id": "fig::overall", "status": "unresolved"}}
    assert "未能完成核对" in engine._figure_sentence(unfinished, {"main_figures": [{}]})
    assert "结果一致" not in engine._figure_sentence(unfinished, {"main_figures": [{}]})
    numbered = {"fig::2": {"id": "fig::2", "status": "cleared"}, **finals}
    assert "Fig. 2" in engine._figure_sentence(numbered, {}) and "overall" not in engine._figure_sentence(numbered, {})


def test_judge_prompt_asks_for_refutation_with_the_category_checklist():
    from review_v3 import prompts
    text = prompts.judge({"doi": "10.1/x", "paper": {"title": "t"}},
                         [{"id": "a", "category": "S1", "dimension": "data", "location": "p1", "issue_type": "dup"}], [])
    assert "尝试推翻" in text and "换算网格或地板值" in text and "同一个量、同一统计量、同一汇总范围" in text
    assert "没有核完的替代解释不算已排除" in text
    assert engine.calibration_version() != engine._digest(
        [p for p in config.RULES_DIR.glob("*.md") if p.name not in engine.REPORT_RULES]
        + [Path(engine.__file__).with_name("categories.py")], f"judge:3:{config.JUDGE_MODEL}")
