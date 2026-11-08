"""Many issues are graded by several calibrators in parallel; calibration
stops early enough for the judge; the calibration box is at least ten
minutes from its own start."""
import asyncio
import json
import time

from review_v3 import config, engine, ledger, session


def finding(item_id, dimension="data"):
    return {"id": item_id, "status": "issue", "severity": "minor", "dimension": dimension,
            "title": item_id, "location": "Table 1 p2", "issue_type": "孤立单格错误",
            "finding": "表中数值为 10，源数据正确值为 11", "why": "单格错误不影响核心比较",
            "basis": "已核对原表格和源数据，其余数值一致"}


def grade_all(path):
    for item in ledger.open_items(ledger.load(path)):
        result = ledger.update(path, {"id": item["id"], "status": "issue", "category": "M1",
                                     "why": "孤立单格且不改变核心比较结论"}, "calibrator")
        assert result == (1, []), result


def test_groups_keep_dimensions_together_within_the_batch_size():
    records = [finding(f"d{i}") for i in range(20)] + [finding(f"i{i}", "image") for i in range(3)] \
        + [finding(f"r{i}", "ref") for i in range(2)] + [finding(f"m{i}", "methodology") for i in range(5)]
    groups = engine._calibration_groups(records, 12)
    assert [len(g) for g in groups] == [12, 11, 7]
    assert all(len(g) <= 12 for g in groups)
    assert sorted(r["id"] for g in groups for r in g) == sorted(r["id"] for r in records)
    for g in groups:  # a dimension is never split across batches unless it alone exceeds the size
        dims = [r["dimension"] for r in g]
        for d in set(dims):
            assert dims.index(d) + dims.count(d) == len(dims) - sum(1 for x in dims[dims.index(d):] if x != d)
    assert engine._calibration_groups(records[:5], 12) == [records[:5]]


def test_calibration_stops_before_the_judge_reserve(monkeypatch):
    from api.services.review_runtime import api_review_budget
    review = engine.Review("x", "", "/tmp", engine.Path("/tmp/run_x"), time.monotonic() + 1000)
    token = api_review_budget.set(True)
    try:
        assert review._hard_deadline("calibrate") == review.deadline - config.JUDGE_RESERVE_SECONDS
        assert review._hard_deadline("judge") == review.deadline
        assert review._hard_deadline("investigate") == review.deadline
    finally:
        api_review_budget.reset(token)
    assert review._hard_deadline("calibrate") == review.deadline - max(config.GRADING_RESERVE_SECONDS,
                                                                      config.JUDGE_RESERVE_SECONDS)
    assert config.PHASE_FLOOR_SECONDS["calibrate"] == 10 * 60


def test_many_issues_are_graded_in_parallel_batches(tmp_path, monkeypatch):
    ids = [f"d{i}" for i in range(18)] + [f"m{i}" for i in range(7)]
    finals = {i: finding(i, "methodology" if i.startswith("m") else "data") for i in ids}
    calls = []

    async def fake_run(spec):
        calls.append((spec.name, len(ledger.load(spec.ledger_path)["items"])))
        await asyncio.sleep(0)
        grade_all(spec.ledger_path)
        return session.SessionOutcome("done")
    monkeypatch.setattr(session, "run", fake_run)

    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path / "run", None)
        out = await review.calibrate(finals)
        assert sorted(n for n, _ in calls) == ["cal_b1", "cal_b2", "cal_b3"]
        assert all(size <= config.CALIBRATE_BATCH for _, size in calls) and sum(s for _, s in calls) == 25
        assert all(out[i]["category"] == "M1" for i in ids)
        assert review.cal_status == "done" and review.calibration_metrics["pending_ids"] == []
        assert review.calibration_metrics["batches"] == 3
        assert not (review.run_dir / "sessions/cal_all.json").exists()
        assert not (review.run_dir / "sessions/cal_rest.json").exists()
        merged = ledger.load(review.run_dir / "ledgers/cal_all.json")["items"]
        assert all(merged[i]["category"] == "M1" for i in ids)
        await review.close()
    asyncio.run(run())


def test_batch_leftovers_go_to_one_recovery_session(tmp_path, monkeypatch):
    ids = [f"d{i}" for i in range(14)]
    finals = {i: finding(i) for i in ids}
    calls = []

    async def fake_run(spec):
        calls.append(spec.name)
        if spec.name == "cal_b2":  # grades nothing: its items stay open
            return session.SessionOutcome("stalled")
        grade_all(spec.ledger_path)
        return session.SessionOutcome("done")
    monkeypatch.setattr(session, "run", fake_run)

    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path / "run", None)
        out = await review.calibrate(finals)
        assert calls == ["cal_b1", "cal_b2", "cal_rest"] or calls == ["cal_b2", "cal_b1", "cal_rest"]
        assert all(out[i]["category"] == "M1" for i in ids)
        assert review.cal_status == "done"
        rest = json.loads((review.run_dir / "sessions/cal_rest.json").read_text())
        assert rest["continuation_mode"] == "restart"  # no single cal_all context to carry
        assert len(ledger.load(review.run_dir / "ledgers/cal_rest.json")["items"]) == 2
        await review.close()
    asyncio.run(run())


def test_small_batches_still_use_one_calibrator(tmp_path, monkeypatch):
    ids = [f"d{i}" for i in range(config.CALIBRATE_BATCH)]
    calls = []

    async def fake_run(spec):
        calls.append(spec.name); grade_all(spec.ledger_path); return session.SessionOutcome("done")
    monkeypatch.setattr(session, "run", fake_run)

    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path / "run", None)
        out = await review.calibrate({i: finding(i) for i in ids})
        assert calls == ["cal_all"] and all(out[i]["category"] == "M1" for i in ids)
        await review.close()
    asyncio.run(run())


def test_calibrators_write_from_the_second_chunk_on(tmp_path, monkeypatch):
    """After one chunk of reading, continuation prompts tell the calibrator
    to classify from the records and write, not to reopen the paper."""
    seen = {}

    async def fake_run(spec):
        seen[spec.name] = spec.continue_prompt({"chunks": 1})
        grade_all(spec.ledger_path)
        return session.SessionOutcome("done")
    monkeypatch.setattr(session, "run", fake_run)

    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path / "run", None)
        await review.calibrate({f"d{i}": finding(f"d{i}") for i in range(3)})
        await review.close()
        review2 = engine.Review("y", "", str(tmp_path), tmp_path / "run2", None)
        await review2.calibrate({f"d{i}": finding(f"d{i}") for i in range(20)})
        await review2.close()
    asyncio.run(run())
    assert "cal_all" in seen and "cal_b1" in seen
    for text in seen.values():
        assert "不要再打开原文" in text


def test_calibrator_reading_chunk_is_capped_and_follow_up_visual_pass_is_short(tmp_path, monkeypatch):
    seen = {}

    async def fake_run(spec):
        seen[spec.name] = (spec.chunk_seconds, spec.max_chunks)
        grade_all(spec.ledger_path)
        return session.SessionOutcome("done")
    monkeypatch.setattr(session, "run", fake_run)

    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path / "run", None)
        await review.calibrate({f"d{i}": finding(f"d{i}") for i in range(3)})
        await review.close()
        review2 = engine.Review("y", "", str(tmp_path), tmp_path / "run2", None)
        await review2.calibrate({f"d{i}": finding(f"d{i}") for i in range(20)})
        await review2.close()
    asyncio.run(run())
    assert seen["cal_all"][0] == config.CALIBRATE_CHUNK_SECONDS == 240
    assert seen["cal_b1"][0] == 240 and seen["cal_b2"][0] == 240
    # the cap shortens an ordinary chunk but never the wrap-up chunk or a nearer deadline
    spec = session.SessionSpec("cal_x", tmp_path, "", "calibrator", chunk_seconds=240)
    assert session.chunk_wall_limit(spec, wrapping=False, now=1000.0) == 240
    assert session.chunk_wall_limit(spec, wrapping=True, now=1000.0) == config.WRAPUP_SECONDS
    spec.deadline = 1100.0
    assert session.chunk_wall_limit(spec, wrapping=False, now=1000.0) == 100.0
    plain = session.SessionSpec("inv_x", tmp_path, "", "investigator")
    assert session.chunk_wall_limit(plain, wrapping=False, now=1000.0) == config.CHUNK_TIMEOUT_SECONDS
    # follow-ups: the visual pass gets a short budget, other shards the size-based one
    assert config.complete_chunks(1, "image_scan") == config.COMPLETE_CHUNKS_IMAGE_SCAN == 3
    assert config.complete_chunks(1, "stats") == 2
    assert config.complete_chunks(4, "methodology") == 3
    assert config.complete_chunks(9, "data") == config.MAX_CHUNKS_COMPLETE


def test_follow_ups_have_a_chunk_cap_and_the_visual_pass_one_round(tmp_path, monkeypatch):
    """Follow-up sessions get COMPLETE_CHUNK_SECONDS per chunk; the visual
    pass is followed up in round 1 only, other shards may get round 2."""
    seen = []

    async def fake_run(spec):
        items = list(ledger.load(spec.ledger_path)["items"])
        seen.append((spec.name, spec.chunk_seconds, spec.max_chunks, items))
        return session.SessionOutcome("exhausted")  # writes nothing: items stay open
    monkeypatch.setattr(session, "run", fake_run)
    monkeypatch.setattr(engine, "_sync_visual_progress", lambda run_dir, finals: finals)

    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path / "run", None)
        review.shards = [{"id": "image_scan", "kind": "image_scan", "dimension": "image",
                          "items": [{"id": "image::visual_pass", "kind": "visual_pass", "required": True, "title": "逐页浏览"}]},
                         {"id": "data", "kind": "data", "dimension": "data",
                          "items": [{"id": "trailing_digit::book.xlsx::S1", "kind": "trailing_digit", "required": True, "title": "末位数字"}]}]
        finals = {"image::visual_pass": {"id": "image::visual_pass", "status": "unresolved", "dimension": "image"},
                  "trailing_digit::book.xlsx::S1": {"id": "trailing_digit::book.xlsx::S1", "status": "unresolved", "dimension": "data"}}
        await review.complete(finals)
        await review.close()
    asyncio.run(run())
    assert all(c == config.COMPLETE_CHUNK_SECONDS == 300 for _, c, _, _ in seen)
    visual = [(n, m) for n, _, m, items in seen if "image::visual_pass" in items]
    assert len(visual) == 1 and visual[0][0].startswith("fin1_")      # one round only for the visual pass
    assert visual[0][1] == config.COMPLETE_CHUNKS_IMAGE_SCAN
    data = [n for n, _, _, items in seen if "trailing_digit::book.xlsx::S1" in items]
    assert [n[:4] for n in data] == ["fin1", "fin2"]                   # other shards still get round 2


def test_write_only_mode_lets_only_ledger_commands_through(monkeypatch):
    from review_v3.tools import guard
    monkeypatch.setenv("PV3_WRITE_ONLY", "1")
    monkeypatch.setenv("PV3_ACTOR", "calibrator")
    ok = ["pv3 ledger update <<'EOF'\n[{\"id\": \"x\"}]\nEOF", "pv3 ledger show --open", "pv3 item 'fig::2'",
          "cd /tmp && pv3 ledger update <<'EOF'\n[]\nEOF", "timeout 60 pv3 ledger show"]
    for command in ok:
        assert guard.check("Bash", {"command": command}) is None, command
    blocked = ["python3 -I -c 'import openpyxl'", "pv3 render main 5 --clip 1,2,3,4", "pv3 page main 3",
               "pv3 sheet book.xlsx S1", "ls", "cat /tmp/x.py; pv3 ledger show"]
    for command in blocked:
        assert "只写入台账" in (guard.check("Bash", {"command": command}) or ""), command
    assert "只写入台账" in guard.check("Read", {"file_path": "/tmp/x.png"})
    monkeypatch.delenv("PV3_WRITE_ONLY")
    assert guard.check("Bash", {"command": "pv3 page main 3"}) is None


def test_calibrators_and_judge_switch_to_write_only(tmp_path, monkeypatch):
    seen = {}

    async def fake_run(spec):
        seen[spec.name] = (spec.write_only_after, spec.chunk_seconds)
        grade_all(spec.ledger_path)
        return session.SessionOutcome("done")
    monkeypatch.setattr(session, "run", fake_run)

    async def run():
        review = engine.Review("x", "", str(tmp_path), tmp_path / "run", None)
        await review.calibrate({f"d{i}": finding(f"d{i}") for i in range(20)})
        await review.close()
    asyncio.run(run())
    assert seen["cal_b1"] == (1, config.CALIBRATE_CHUNK_SECONDS) and seen["cal_b2"][0] == 1
    spec = session.SessionSpec("cal_x", tmp_path, "", "calibrator", write_only_after=1)
    assert not session.write_only(spec, 0) and session.write_only(spec, 1) and session.write_only(spec, 3)
    assert not session.write_only(session.SessionSpec("inv_x", tmp_path, "", "investigator"), 5)
