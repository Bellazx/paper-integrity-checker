"""Exercise capture on saved findings. Read-only inputs, new outputs, no model calls.

This checks evidence handling, not scientific conclusions or end-to-end speed.
"""
import argparse
import asyncio
import json
from pathlib import Path
import time
from unittest.mock import patch

from PIL import Image, ImageDraw
from review_v3 import engine, session


async def run(source: Path, output: Path):
    output.mkdir(parents=True, exist_ok=False)
    results = []
    async def no_model(spec):
        return session.SessionOutcome("exhausted")
    with patch.object(session, "run", no_model):
        for key in ("098f7641", "81444ed4", "0289f47c", "b79777e8", "1d91ffd5"):
            old = json.loads((source / key / "review.json").read_text())
            previous = Path(old["_review_workdir"])
            finals = json.loads((previous / "finals.json").read_text())["finals"]
            dossier = json.loads((previous / "dossier/index.json").read_text())
            review = engine.Review(old["doi"], "", old["_input_dir"], output / key, None)
            review.dossier = dossier
            start = time.monotonic()
            try:
                captured = await review.capture(finals)
            finally:
                await review.close()
            for item_id, record in finals.items():
                assert {k: v for k, v in captured[item_id].items() if k != "evidence"} == {
                    k: v for k, v in record.items() if k != "evidence"}
            images = sorted({ev["path"] for record in captured.values() if engine._is_issue(record, "substantive")
                             for ev in record.get("evidence") or [] if ev.get("path") and Path(ev["path"]).is_file()})
            tiles = []
            for path in images:
                with Image.open(path) as image:
                    image.load()
                    thumbnail = image.convert("RGB"); thumbnail.thumbnail((450, 310))
                    tile = Image.new("RGB", (480, 350), "white")
                    tile.paste(thumbnail, (5, 25))
                    ImageDraw.Draw(tile).text((5, 5), Path(path).name[:65], fill="black")
                    tiles.append(tile)
            if tiles:
                sheet = Image.new("RGB", (480 * 3, 350 * ((len(tiles) + 2) // 3)), "#dddddd")
                for n, tile in enumerate(tiles): sheet.paste(tile, ((n % 3) * 480, (n // 3) * 350))
                sheet.save(output / key / "contact.png")
            summary = {"key": key, "seconds": time.monotonic() - start,
                       "substantive": sum(engine._is_issue(r, "substantive") for r in finals.values()),
                       "missing_before": engine._evidence_gaps(finals, review.input_dir),
                       "missing_after": engine._evidence_gaps(captured, review.input_dir),
                       "readable_images": len(images), "findings_unchanged": True}
            (output / key).mkdir(exist_ok=True)
            (output / key / "captured.json").write_text(json.dumps(captured, ensure_ascii=False, indent=2))
            results.append(summary)
            (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
            print(key, "seconds", round(summary["seconds"], 2), "gaps", len(summary["missing_before"]),
                  "->", len(summary["missing_after"]), "readable images", len(images), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.source, args.output))
