import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.pdf_table_extractor import extract_tables_from_pdf


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf")
    parser.add_argument("output")
    parser.add_argument("--timeout", type=int, default=5)
    args = parser.parse_args()

    started = time.monotonic()
    written = extract_tables_from_pdf(
        args.pdf,
        args.output,
        page_strategy_timeout_seconds=args.timeout,
    )
    print(f"elapsed={time.monotonic() - started:.2f}")
    print(f"tables={len(written)}")
    for path in written:
        print(Path(path))


if __name__ == "__main__":
    main()
