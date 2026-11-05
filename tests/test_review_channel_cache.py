import json

from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from api.services.review import _augment_suspicious_same_group_channel_duplicates


def test_channel_cache_preserves_pairwise_missing_rows_and_filters_integer_pairs(tmp_path, monkeypatch):
    book = Workbook()
    sheet = book.active
    sheet.title = 'data'
    for col in (1, 7, 13, 19):
        sheet.cell(1, col, f'channel {col}')
    for row in range(4, 24):
        for col in (1, 7):
            sheet.cell(row, col, row + 0.125)
        for col in (13, 19):
            sheet.cell(row, col, row)
    sheet.cell(5, 7).value = None
    book.save(tmp_path / 'values.xlsx')
    pairs = [dict(file='values.xlsx', sheet='data', n_points=20,
                  col_a_idx=a, col_b_idx=b, data_start_row=3)
             for a, b in [(0, 6), (12, 18), (0, 12), (6, 18), (12, 18)]]
    evidence = tmp_path / 'evidence.json'
    evidence.write_text(json.dumps({'deterministic_findings': {'duplicate_column_pairs': pairs}}))
    original = Worksheet.max_row.fget
    calls = []

    def max_row(ws):
        calls.append(ws.title)
        return original(ws)

    monkeypatch.setattr(Worksheet, 'max_row', property(max_row))
    _augment_suspicious_same_group_channel_duplicates(evidence, str(tmp_path))
    result = json.loads(evidence.read_text())
    # openpyxl may also inspect dimensions once while opening the workbook.
    assert len(calls) <= 2
    assert len(result['high_findings']) == 1
    assert result['_project_channel_duplicate_findings'][0]['n_values'] == 19
