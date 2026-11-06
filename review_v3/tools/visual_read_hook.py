"""Record a successful Read of a registered page image, without judging it."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from review_v3 import visual_progress


def main():
    try:
        event = json.load(sys.stdin)
        if event.get("hook_event_name") != "PostToolUse" or event.get("tool_name") != "Read":
            return
        response = event.get("tool_response")
        if isinstance(response, dict) and (response.get("is_error") or response.get("error")):
            return
        workdir = os.environ.get("PV3_WORKDIR")
        image = (event.get("tool_input") or {}).get("file_path")
        if workdir and image:
            visual_progress.record_read(Path(workdir), Path(image), os.environ.get("PV3_ACTOR", "agent"))
    except (OSError, ValueError, TypeError):
        # Failed recording leaves the page pending; it must not break Read.
        pass


if __name__ == "__main__":
    main()
