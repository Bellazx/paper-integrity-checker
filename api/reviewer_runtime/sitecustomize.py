"""Runtime safety rails for Python snippets launched by review agents.

This module is loaded only when the API explicitly adds this directory to a
review subprocess's ``PYTHONPATH``.  It does not affect the API, detector, or
normal command-line tools.
"""

from __future__ import annotations

import os


if os.environ.get("PAPER_REVIEW_GUARD_READONLY_CELL", "0") == "1":
    try:
        from openpyxl.worksheet._read_only import ReadOnlyWorksheet

        _original_cell = ReadOnlyWorksheet.cell
        _cell_call_counts: dict[int, int] = {}
        _cell_row_costs: dict[int, int] = {}
        _cell_call_limit = max(
            1,
            int(os.environ.get("PAPER_REVIEW_READONLY_CELL_LIMIT", "256")),
        )
        _cell_row_cost_limit = max(
            1,
            int(os.environ.get("PAPER_REVIEW_READONLY_CELL_ROW_COST_LIMIT", "100000")),
        )

        def _guarded_cell(self, *args, **kwargs):
            key = id(self)
            count = _cell_call_counts.get(key, 0) + 1
            _cell_call_counts[key] = count
            row = kwargs.get("row", args[0] if args else 1)
            try:
                row_cost = max(1, int(row))
            except (TypeError, ValueError):
                row_cost = 1
            cumulative_row_cost = _cell_row_costs.get(key, 0) + row_cost
            _cell_row_costs[key] = cumulative_row_cost
            if count > _cell_call_limit or cumulative_row_cost > _cell_row_cost_limit:
                raise RuntimeError(
                    "read_only worksheet cell() scan budget exceeded; "
                    "load the required range once with iter_rows(values_only=True)"
                )
            return _original_cell(self, *args, **kwargs)

        ReadOnlyWorksheet.cell = _guarded_cell
    except (ImportError, ValueError):
        # Non-review Python tools may not have openpyxl installed.  The guard
        # must never make Python itself unavailable.
        pass
