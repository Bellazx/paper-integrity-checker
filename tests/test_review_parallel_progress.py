"""Concurrency, cancellation and durable coverage tests; no remote model calls."""
import asyncio
import copy
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from review_v3 import config, engine, ledger, session, visual_progress as visual


def finding(item_id="high::1"):
    return {"id": item_id, "status": "issue", "severity": "substantive", "dimension": "data",
            "location": "Fig. 1 p1", "issue_type": "数值互相矛盾", "finding": "两处测量结果分别是 10 和 30",
            "why": "影响论文的核心比较结果", "basis": "对照原文图和表中的数值"}


def fake_capture(monkeypatch):
    monkeypatch.setattr(engine.autocrop, "crop_for", lambda *a, **k: [])
    monkeypatch.setattr(engine.autocrop, "cells_for", lambda *a, **k: [])


def save_crop(spec, tmp_path):
    item = next(iter(ledger.load(spec.ledger_path)["items"].values()))
    crop = tmp_path / f"{spec.name}_c1_2_30_40_d110.png"
    crop.write_bytes(b"image")
    record = {**item["first"], "id": item["id"], "evidence": [{"path": str(crop), "caption": "原文对照"}]}
    assert ledger.update(spec.ledger_path, [record], "evidence")[0] == 1
    return crop


def test_capture_overlaps_grading_and_preserves_final_verdict(tmp_path, monkeypatch):
    fake_capture(monkeypatch)

    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path / "run", None)
        capture_started, grading_started = asyncio.Event(), asyncio.Event()

        async def capture(spec):
            capture_started.set()
            await asyncio.wait_for(grading_started.wait(), 2)
            save_crop(spec, tmp_path)

        monkeypatch.setattr(session, "run", capture)
        original = finding()
        review.offer_capture([original])
        await asyncio.wait_for(capture_started.wait(), 2)
        # Grading can proceed while capture is still awaiting this event.
        final = {**original, "severity": "minor", "category": "M1"}
        grading_started.set()
        await asyncio.gather(*review.capture_tasks.values())
        out = await review.capture({original["id"]: final})
        assert out[original["id"]]["severity"] == "minor"
        assert out[original["id"]]["category"] == "M1"
        assert out[original["id"]]["evidence"]
        assert "evidence" not in original  # snapshot, no shared mutation
        await review.close()
    asyncio.run(run())


def test_capture_versions_do_not_attach_to_changed_or_cleared_findings(tmp_path, monkeypatch):
    fake_capture(monkeypatch)

    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path / "run", None)
        seen = []
        async def capture(spec):
            seen.append(save_crop(spec, tmp_path))
        monkeypatch.setattr(session, "run", capture)
        old = finding()
        review.offer_capture([old, old])
        await asyncio.gather(*review.capture_tasks.values())
        changed = {**old, "location": "Fig. 2 p2"}
        assert not engine._recover_capture(review.run_dir, {old["id"]: changed})[old["id"]].get("evidence")
        out = await review.capture({old["id"]: changed})
        assert len(seen) == 2
        assert [e["path"] for e in out[old["id"]]["evidence"]] == [str(seen[-1])]
        cleared = {**old, "status": "cleared"}
        assert not engine._recover_capture(review.run_dir, {old["id"]: cleared})[old["id"]].get("evidence")
        await review.close()
    asyncio.run(run())


def test_capture_cancellation_keeps_partial_evidence_and_reaps_tasks(tmp_path, monkeypatch):
    fake_capture(monkeypatch)

    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path / "run", None)
        saved, killed = asyncio.Event(), asyncio.Event()
        async def capture(spec):
            save_crop(spec, tmp_path)
            saved.set()
            try:
                await asyncio.Event().wait()
            finally:
                killed.set()
        monkeypatch.setattr(session, "run", capture)
        record = finding()
        review.offer_capture([record])
        await asyncio.wait_for(saved.wait(), 2)
        await review.close()
        assert killed.is_set()
        assert all(task.done() for task in review.background)
        out = engine._recover_capture(review.run_dir, {record["id"]: record})
        assert out[record["id"]]["evidence"]
        assert out[record["id"]]["severity"] == "substantive"
    asyncio.run(run())


def test_capture_queue_limits_parallelism_and_does_not_start_after_deadline(tmp_path, monkeypatch):
    fake_capture(monkeypatch)
    monkeypatch.setattr(config, "CAPTURE_PARALLEL", 2)

    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path / "run", None)
        active = peak = calls = 0
        release, both = asyncio.Event(), asyncio.Event()
        async def capture(spec):
            nonlocal active, peak, calls
            active += 1; calls += 1; peak = max(active, peak)
            if active == 2:
                both.set()
            await release.wait()
            active -= 1
        monkeypatch.setattr(session, "run", capture)
        review.offer_capture([finding(str(n)) for n in range(5)])
        await asyncio.wait_for(both.wait(), 2)
        review.deadline = time.monotonic() - 1
        release.set()
        await asyncio.gather(*review.capture_tasks.values())
        assert calls == peak == 2
        review.offer_capture([finding("late")])
        assert len(review.capture_tasks) == 5
        await review.close()
    asyncio.run(run())


def test_watcher_captures_a_finding_before_investigation_finishes(tmp_path, monkeypatch):
    fake_capture(monkeypatch)
    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path / "run", None)
        path = review.ledger_path("inv", "data")
        record = finding()
        ledger.create(path, {}, [record, {"id": "still-investigating"}])
        captured = asyncio.Event()
        async def capture(spec):
            save_crop(spec, tmp_path)
            captured.set()
        monkeypatch.setattr(session, "run", capture)
        task = asyncio.create_task(review._watch_capture())
        review.background.append(task)
        await asyncio.wait_for(captured.wait(), 2)
        assert ledger.open_items(ledger.load(path))
        await review.close()
    asyncio.run(run())


def make_pages(tmp_path, count=3):
    import fitz
    pdf = tmp_path / "main.pdf"
    with fitz.open() as doc:
        for n in range(count):
            doc.new_page().insert_text((50, 50), f"Figure {n + 1}")
        doc.save(pdf)
    dossier = {"docs": [{"id": "main", "path": str(pdf), "pages": count}],
               "captions": [{"doc": "main", "page": 2, "label": "Fig. 2", "kind": "figure"}]}
    visual.initialize(tmp_path, dossier)
    return dossier


def observe(tmp_path, page, actor="investigator:image", **kwargs):
    image = tmp_path / f"main_p{page:03d}_full_d110.png"
    image.write_bytes(b"image")
    visual.register_render(tmp_path, "main", page, image, full=True)
    visual.record_read(tmp_path, image, actor)
    return visual.update(tmp_path, [{"doc": "main", "page": page, "status": "reviewed", "result": "clear",
        "note": "实际浏览该页全部面板，未见明显异常", **kwargs}], actor)


def test_render_alone_cannot_close_page_or_overall_visual_pass(tmp_path):
    make_pages(tmp_path)
    image = tmp_path / "page.png"; image.write_bytes(b"image")
    visual.register_render(tmp_path, "main", 1, image, full=True)
    entry = {"doc": "main", "page": 1, "status": "reviewed", "result": "clear", "note": "本页未发现明显异常或重复"}
    assert visual.update(tmp_path, [entry], "actor")[0] == 0
    visual.record_read(tmp_path, image, "other-actor")
    assert visual.update(tmp_path, [entry], "actor")[0] == 0
    path = tmp_path / "ledgers" / "inv_image.json"
    ledger.create(path, {}, [{"id": "image::visual_pass"}])
    closed, errors = ledger.update(path, [{"id": "image::visual_pass", "status": "cleared",
        "basis": "声称全部页面检查完毕，未见异常"}], "actor")
    assert closed == 0 and "逐页浏览尚未完成" in errors[0]


def test_progress_survives_restart_and_only_remaining_pages_are_scheduled(tmp_path):
    dossier = make_pages(tmp_path)
    assert observe(tmp_path, 1)[0] == 1
    assert observe(tmp_path, 2, status="needs_detail", note="Fig.2 面板 a 和 b 尚需放大对照")[0] == 1
    visual.initialize(tmp_path, dossier)
    assert [p["page"] for p in visual.pending(visual.load(tmp_path))] == [2, 3]
    assert "面板 a 和 b" in visual.pending_note(tmp_path)
    assert observe(tmp_path, 2, "challenger:image")[0] == 1
    assert len(visual.load(tmp_path)["pages"]["main:2"]["observations"]) == 2
    finals = {"image::visual_pass": {"id": "image::visual_pass", "status": "cleared", "dimension": "image"}}
    partial = engine._sync_visual_progress(tmp_path, finals)
    assert partial["image::visual_pass"]["status"] == "unresolved"
    assert "main p3" in partial["image::visual_pass"]["note"]
    observe(tmp_path, 3)
    assert engine._sync_visual_progress(tmp_path, partial)["image::visual_pass"]["status"] == "cleared"


def test_page_writes_are_atomic_and_new_source_invalidates_progress(tmp_path):
    dossier = make_pages(tmp_path, 8)
    with ThreadPoolExecutor(4) as pool:
        assert all(n == 1 and not errors for n, errors in pool.map(lambda n: observe(tmp_path, n), range(1, 9)))
    assert visual.summary(tmp_path)["reviewed"] == 8
    # New source bytes invalidate even a previous successful observation.
    Path(dossier["docs"][0]["path"]).write_bytes(b"new source")
    visual.initialize(tmp_path, dossier)
    assert visual.summary(tmp_path)["reviewed"] == 0
    assert not visual.load(tmp_path)["renders"]


def test_progress_extends_productive_session_without_credit_for_rewritten_notes(tmp_path):
    make_pages(tmp_path)
    path = tmp_path / "ledgers" / "inv_image.json"
    ledger.create(path, {}, [{"id": "image::visual_pass"}])
    spec = session.SessionSpec("s", tmp_path, "", "investigator:image", ledger_path=path)
    before = session._progress_key(spec)
    observe(tmp_path, 1)
    after = session._progress_key(spec)
    assert after[:2] == before[:2] and after[2] > before[2]
    observe(tmp_path, 1)
    assert session._progress_key(spec) == after


def test_successful_read_hook_is_required_and_survives_process_boundary(tmp_path):
    make_pages(tmp_path)
    image = tmp_path / "page.png"; image.write_bytes(b"image")
    visual.register_render(tmp_path, "main", 1, image, full=True)
    env = {**os.environ, "PV3_WORKDIR": str(tmp_path), "PV3_ACTOR": "test"}
    event = {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {"file_path": str(image)},
             "tool_response": {"type": "image"}}
    subprocess.run([config.PYTHON, "-I", str(config.PACKAGE_DIR / "tools/visual_read_hook.py")],
                   input=json.dumps(event), text=True, env=env, check=True)
    entry = {"doc": "main", "page": 1, "status": "reviewed", "result": "clear", "note": "已读取该页图像，面板未见异常"}
    assert visual.update(tmp_path, [entry], "test")[0] == 1


def test_page_scope_does_not_expand_a_chapter_to_the_whole_volume(tmp_path):
    dossier = make_pages(tmp_path, 8)
    dossier["scope"] = {"doc": "main", "first": 3, "last": 5}
    visual.initialize(tmp_path, dossier)
    assert [p["page"] for p in visual.pending(visual.load(tmp_path))] == [3, 4, 5]


def test_followup_new_findings_are_kept(tmp_path, monkeypatch):
    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path, None)
        review.shards = [{"id": "image_scan", "kind": "image_scan", "dimension": "image",
                          "items": [{"id": "image::visual_pass", "title": "逐页浏览"}]}]
        async def followup(spec):
            new = {**finding("new:panel_duplicate"), "dimension": "image"}
            entries = [{"id": "image::visual_pass", "status": "cleared", "basis": "剩余页面全部核查完毕，发现问题已单独记录"}, new]
            assert ledger.update(spec.ledger_path, entries, spec.actor)[0] == 2
        monkeypatch.setattr(session, "run", followup)
        finals = {"image::visual_pass": {"id": "image::visual_pass", "status": "unresolved", "dimension": "image"}}
        out = await review.complete(finals)
        assert out["new:panel_duplicate"]["severity"] == "substantive"
        assert out["image::visual_pass"]["status"] == "cleared"
    asyncio.run(run())


def test_evidence_failure_does_not_abort_grading_or_report(tmp_path, monkeypatch):
    fake_capture(monkeypatch)
    async def fail(spec):
        raise RuntimeError("model unavailable")
    monkeypatch.setattr(session, "run", fail)
    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path / "run", None)
        record = finding()
        out = await review.capture({record["id"]: record})
        assert out[record["id"]]["severity"] == "substantive"
        assert engine._evidence_gaps(out, str(tmp_path)) == [{"id": "high::1", "missing": ["pdf"]}]
        await review.close()
    asyncio.run(run())


def test_failed_read_hook_does_not_count_and_crop_is_not_a_full_page(tmp_path):
    make_pages(tmp_path)
    image = tmp_path / "crop.png"; image.write_bytes(b"image")
    visual.register_render(tmp_path, "main", 1, image, full=False)
    visual.record_read(tmp_path, image, "actor")
    entry = {"doc": "main", "page": 1, "status": "reviewed", "result": "clear", "note": "已读取该页图像，面板未见异常"}
    assert visual.update(tmp_path, [entry], "actor")[0] == 0
    visual.register_render(tmp_path, "main", 1, image, full=True)
    env = {**os.environ, "PV3_WORKDIR": str(tmp_path), "PV3_ACTOR": "actor"}
    for hook, response in [("PreToolUse", {}), ("PostToolUse", {"is_error": True})]:
        event = {"hook_event_name": hook, "tool_name": "Read", "tool_input": {"file_path": str(image)},
                 "tool_response": response}
        subprocess.run([config.PYTHON, "-I", str(config.PACKAGE_DIR / "tools/visual_read_hook.py")],
                       input=json.dumps(event), text=True, env=env, check=True)
    assert visual.update(tmp_path, [entry], "actor")[0] == 0


@pytest.mark.parametrize("language", ["zh", "en", "both"])
@pytest.mark.parametrize("risk,skip", [("高风险", True), ("高风险", False), ("低风险", False)])
def test_api_skips_only_intended_initial_reports_and_keeps_final_languages(tmp_path, monkeypatch, language, risk, skip):
    from unittest.mock import AsyncMock
    from api.models import PaperInfo
    from api.services import detection_reports_worker as worker
    from tests.test_api_paper_deadline import _TaskCapture
    output = tmp_path / "output"; output.mkdir()
    findings = {"paper": {"doi": "10.1/test"}, "summary": {}, "pdf_generated": not skip,
                "pdf_skipped_high_risk": skip}
    (output / "report.json").write_text(json.dumps(findings))
    paper = PaperInfo(doi_slug="10.1_test", doi="10.1/test", input_dir=str(tmp_path), output_dir=str(output),
                      submission_no="upload", language=language)
    monkeypatch.setattr(worker, "OUTPUT_DIR", output)
    monkeypatch.setattr(worker, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(worker, "REVIEW_DIR", tmp_path / "review")
    monkeypatch.setattr(worker, "api_dry_run", lambda: True)
    monkeypatch.setattr(worker, "run_detection_single", AsyncMock(return_value=findings))
    reviewer = AsyncMock(return_value={"doi": "10.1/test", "result": "高风险", "trigger": "auto_detection"})
    monkeypatch.setattr(worker, "run_review_single", reviewer)
    zh = worker.REVIEW_DIR / worker._paper_report_namespace("skip-test", paper) / "review_10.1_test.pdf"
    zh.parent.mkdir(parents=True, exist_ok=True); zh.write_bytes(b"%PDF")
    en = tmp_path / "en.pdf"; en.write_bytes(b"%PDF")
    english = AsyncMock(return_value=str(en))
    chinese = AsyncMock(return_value=[str(zh)])
    monkeypatch.setattr(worker, "generate_english_report", english)
    monkeypatch.setattr(worker, "generate_reports", chinese)
    monkeypatch.setattr("modules.chinese_report_generator._compute_overall_risk", lambda f: {"level": risk})
    tm = _TaskCapture()
    asyncio.run(worker._run_inner("skip-test", tm, [paper], {"max_workers": 1}))
    assert tm.updates[-1]["status"].value == "completed"
    kinds = [call.kwargs["kind"] for call in english.await_args_list]
    wants_en = language in {"en", "both"}
    assert kinds.count("initial") == int(wants_en and not skip)
    assert kinds.count("review") == int(wants_en and risk == "高风险")
    assert chinese.await_count == int(language in {"zh", "both"} and risk == "高风险")
    assert reviewer.await_count == int(risk == "高风险")
    if skip:
        record = tm.updates[-1]["result"]["detection_reports"][0]
        assert not record["chushen_report_url"] and not record["english_chushen_report_url"]
        assert record["processing_completed"]


def test_changed_source_bytes_invalidate_capture_even_with_same_finding(tmp_path, monkeypatch):
    fake_capture(monkeypatch)
    dossier = make_pages(tmp_path)
    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path, None)
        async def capture(spec):
            save_crop(spec, tmp_path)
        monkeypatch.setattr(session, "run", capture)
        record = finding()
        out = await review.capture({record["id"]: record})
        assert out[record["id"]]["evidence"]
        Path(dossier["docs"][0]["path"]).write_bytes(b"changed source")
        visual.initialize(tmp_path, dossier)
        out = engine._recover_capture(tmp_path, {record["id"]: record})
        assert not out[record["id"]].get("evidence")
        await review.close()
    asyncio.run(run())


def test_automatic_capture_failure_still_runs_model_and_preserves_other_carrier(tmp_path, monkeypatch):
    fake_capture(monkeypatch)
    def fail(*args, **kwargs):
        raise ValueError("broken local crop")
    monkeypatch.setattr(engine.autocrop, "crop_for", fail)
    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path / "run", None)
        calls = []
        async def capture(spec):
            calls.append(spec.name)
            save_crop(spec, tmp_path)
        monkeypatch.setattr(session, "run", capture)
        record = finding()
        out = await review.capture({record["id"]: record})
        assert len(calls) == 1 and out[record["id"]]["evidence"]
        await review.close()
    asyncio.run(run())


def test_full_phase_orchestration_starts_capture_before_waiting_for_grading(tmp_path, monkeypatch):
    fake_capture(monkeypatch)
    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path, None)
        record = finding()
        started, graded = asyncio.Event(), asyncio.Event()
        async def capture(spec):
            started.set()
            await asyncio.wait_for(graded.wait(), 2)
            save_crop(spec, tmp_path)
        monkeypatch.setattr(session, "run", capture)
        async def prepare(*args):
            review.shards = [{"id": "data"}]
        async def shard(*args):
            review.shard_finals["data"] = {record["id"]: record}
            return {}
        async def calibrate(finals):
            await asyncio.wait_for(started.wait(), 2)
            graded.set()
            return finals
        async def judge(finals): return finals
        async def write(*args): return {"result": "高风险"}, []
        monkeypatch.setattr(review, "prepare", prepare)
        monkeypatch.setattr(review, "shard_pipeline", shard)
        monkeypatch.setattr(review, "merge", lambda: ({}, []))
        monkeypatch.setattr(review, "calibrate", calibrate)
        monkeypatch.setattr(review, "judge", judge)
        monkeypatch.setattr(review, "write", write)
        try:
            result = await asyncio.wait_for(engine._review_phases(review, "x", {}, "", {},
                tmp_path / "state.json", tmp_path / "final.json", time.monotonic()), 4)
            assert result["result"] == "高风险"
            assert not result["_evidence_gaps"]
        finally:
            await review.close()
    asyncio.run(run())


def test_capture_close_reaps_real_child_process_before_returning(tmp_path, monkeypatch):
    fake_capture(monkeypatch)
    marker, late = tmp_path / "started", tmp_path / "late"
    cli = tmp_path / "fake_cli.py"
    cli.write_text('''import json, os, time
from pathlib import Path
path=Path(os.environ['PV3_LEDGER'])
data=json.loads(path.read_text())
item=next(iter(data['items'].values()))
item.update(item['first'])
crop=Path(os.environ['PV3_WORKDIR'])/'main_p001_c1_2_3_4_d110.png'
crop.write_bytes(b'png')
item['evidence']=[{'path':str(crop),'caption':'saved before cancellation'}]
path.write_text(json.dumps(data))
Path(%r).write_text('ready')
time.sleep(1)
Path(%r).write_text('must not happen')
''' % (str(marker), str(late)))
    wrapper = tmp_path / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec {config.PYTHON} -I {cli} "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setattr(session, "find_cli", lambda: str(wrapper))
    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path / "run", None)
        record = finding()
        review.offer_capture([record])
        async def wait_ready():
            while not marker.exists():
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait_ready(), 3)
        await review.close()
        out = engine._recover_capture(review.run_dir, {record["id"]: record})
        assert out[record["id"]]["evidence"]
        await asyncio.sleep(1.1)
        assert not late.exists()
    asyncio.run(run())


def test_real_render_processes_share_complete_images_and_source_cache(tmp_path):
    from PIL import Image
    dossier = make_pages(tmp_path)
    (tmp_path / "dossier").mkdir(exist_ok=True)
    dossier["docs"][0]["name"] = "main.pdf"
    (tmp_path / "dossier/index.json").write_text(json.dumps(dossier))
    env = {**os.environ, "PV3_WORKDIR": str(tmp_path)}
    def render(_):
        result = subprocess.run([str(config.BIN_DIR / "pv3"), "render", "main", "1"],
                                capture_output=True, text=True, env=env, check=True)
        image = Path(result.stdout.split("  (", 1)[0])
        with Image.open(image) as im:
            im.load()
            assert max(im.size) <= config.RENDER_MAX_SIDE
        return image
    with ThreadPoolExecutor(4) as pool:
        paths = list(pool.map(render, range(4)))
    assert len(set(paths)) == 1
    assert visual.summary(tmp_path)["reviewed"] == 0


def test_page_progress_prompt_does_not_mislabel_productive_chunks_as_stalled():
    from review_v3 import prompts
    text = prompts.investigator_continue({"chunks": 3, "metrics": [{"closed": 0, "visual_progress": 4}]},
                                         ["image::visual_pass"])
    assert "上一段没有写入任何结论" not in text


def test_main_figure_and_page_range_still_require_pdf_when_workbook_exists(tmp_path):
    (tmp_path / "source.xlsx").write_bytes(b"source")
    record = {**finding(), "location": "主图 Fig. 5F（主文第8—9页）；源数据工作表 Figure 5F",
              "evidence": [{"file": "source.xlsx", "sheet": "Figure 5F", "range": "A1:B12"}]}
    assert engine._missing_carriers(record, str(tmp_path)) == {"pdf"}
    record["location"] = "源数据 source.xlsx 工作表 Figure 5F"
    assert engine._missing_carriers(record, str(tmp_path)) == set()
