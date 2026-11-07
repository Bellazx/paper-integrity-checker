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
