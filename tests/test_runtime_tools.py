import pytest

from api.services import runtime_tools


def test_require_rar_tool_returns_backend(monkeypatch):
    expected = {"ok": True, "backend": "7zz", "path": "/usr/local/bin/7zz"}
    monkeypatch.setattr(runtime_tools, "_rar_backend", lambda: expected)

    assert runtime_tools.require_rar_tool() == expected


def test_require_rar_tool_has_actionable_error(monkeypatch):
    monkeypatch.setattr(
        runtime_tools,
        "_rar_backend",
        lambda: {"ok": False, "error": "Cannot find working tool"},
    )

    with pytest.raises(RuntimeError, match="RAR 解压工具不可用"):
        runtime_tools.require_rar_tool()
