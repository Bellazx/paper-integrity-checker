from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import APITimeoutError

from utils import llm_client


PRIMARY_MODEL = "MiniMax-M3.1-Flash"
FALLBACK_MODEL = "MiniMax-M3"


def response(content="result"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.fixture
def create(monkeypatch):
    create = Mock()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm_client, "get_client", lambda: client)
    monkeypatch.setattr(llm_client.time, "sleep", Mock())
    monkeypatch.setattr(llm_client, "MINIMAX_MODEL", PRIMARY_MODEL)
    monkeypatch.setattr(llm_client, "MINIMAX_FALLBACK_MODEL", FALLBACK_MODEL)
    monkeypatch.setattr(llm_client, "LLM_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(llm_client, "LLM_TIMEOUT_FALLBACK_THRESHOLD", 5)
    return create


@pytest.mark.parametrize("timeout_kind", [TimeoutError, APITimeoutError])
def test_fifth_consecutive_timeout_switches_model(create, timeout_kind, caplog):
    timeout = (timeout_kind(request=httpx.Request("POST", "https://example.invalid"))
               if timeout_kind is APITimeoutError else timeout_kind())
    create.side_effect = [timeout] * 5 + [response("<think>hidden</think> result")]

    assert llm_client.chat("paper evidence", system="review", temperature=0.2) == "result"
    calls = [call.kwargs for call in create.call_args_list]
    assert [call["model"] for call in calls] == [PRIMARY_MODEL] * 5 + [FALLBACK_MODEL]
    for key in ("messages", "temperature", "max_tokens"):
        assert calls[-1][key] == calls[0][key]
    assert f"Switching LLM from {PRIMARY_MODEL} to {FALLBACK_MODEL}" in caplog.text


def test_primary_success_before_threshold_does_not_switch(create):
    create.side_effect = [TimeoutError()] * 4 + [response()]
    assert llm_client.chat("paper") == "result"
    assert all(call.kwargs["model"] == PRIMARY_MODEL for call in create.call_args_list)


def test_report_reasoning_effort_is_bounded_and_configurable(create, monkeypatch):
    monkeypatch.delenv('LLM_REASONING_EFFORT', raising=False)
    create.return_value = response()
    assert llm_client.chat('paper') == 'result'
    assert create.call_args.kwargs['reasoning_effort'] == 'low'
    monkeypatch.setenv('LLM_REASONING_EFFORT', 'medium')
    assert llm_client.chat('paper') == 'result'
    assert create.call_args.kwargs['reasoning_effort'] == 'medium'


def test_non_timeout_breaks_consecutive_timeout_streak(create):
    create.side_effect = ([TimeoutError()] * 4 + [ConnectionError("offline")]
                          + [TimeoutError()] * 4 + [response()])
    assert llm_client.chat("paper") == "result"
    assert all(call.kwargs["model"] == PRIMARY_MODEL for call in create.call_args_list)


def test_non_timeout_errors_use_existing_retry_budget(create):
    create.side_effect = ConnectionError("offline")
    with pytest.raises(RuntimeError, match=f"model={PRIMARY_MODEL}") as error:
        llm_client.chat("paper")
    assert isinstance(error.value.__cause__, ConnectionError)
    assert create.call_count == 3


def test_fallback_retries_then_succeeds(create):
    create.side_effect = [TimeoutError()] * 7 + [response()]
    assert llm_client.chat("paper") == "result"
    assert [call.kwargs["model"] for call in create.call_args_list] == [PRIMARY_MODEL] * 5 + [FALLBACK_MODEL] * 3


def test_fallback_exhaustion_is_bounded_and_raises(create):
    create.side_effect = TimeoutError()
    with pytest.raises(RuntimeError, match=f"model={FALLBACK_MODEL}") as error:
        llm_client.chat("paper")
    assert isinstance(error.value.__cause__, TimeoutError)
    assert create.call_count == 8


def test_fallback_state_does_not_leak_between_calls(create):
    create.side_effect = [TimeoutError()] * 5 + [response(), response()]
    assert llm_client.chat("first paper") == "result"
    assert llm_client.chat("second paper") == "result"
    assert create.call_args_list[-1].kwargs["model"] == PRIMARY_MODEL


def test_timeouts_do_not_accumulate_across_successful_calls(create):
    create.side_effect = [TimeoutError()] * 4 + [response()] + [TimeoutError(), response()]
    assert llm_client.chat("first paper") == "result"
    assert llm_client.chat("second paper") == "result"
    assert all(call.kwargs["model"] == PRIMARY_MODEL for call in create.call_args_list)


def test_report_deadline_caps_each_request_and_prevents_retry(create, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(llm_client.time, 'monotonic', lambda: now[0])
    monkeypatch.setenv('PAPER_LLM_DEADLINE', '104')

    def timeout(**kwargs):
        assert kwargs['timeout'] == 4
        now[0] = 104
        raise TimeoutError()

    create.side_effect = timeout
    with pytest.raises(TimeoutError, match='budget exhausted'):
        llm_client.chat('paper')
    assert create.call_count == 1


def test_configured_primary_is_already_fallback(create, monkeypatch):
    monkeypatch.setattr(llm_client, "MINIMAX_MODEL", FALLBACK_MODEL)
    create.side_effect = TimeoutError()
    with pytest.raises(RuntimeError, match=f"model={FALLBACK_MODEL}"):
        llm_client.chat("paper")
    assert create.call_count == 3


@pytest.mark.parametrize("content", ["", None, "<think>only reasoning</think>"])
def test_empty_content_does_not_trigger_timeout_fallback(create, content):
    create.side_effect = [response(content)] * 3
    with pytest.raises(RuntimeError, match=f"model={PRIMARY_MODEL}"):
        llm_client.chat("paper")
    assert create.call_count == 3


def test_client_disables_hidden_sdk_retries(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(llm_client, "_client", None)
    monkeypatch.setattr(llm_client, "OpenAI", factory)
    assert llm_client.get_client() is factory.return_value
    assert llm_client.get_client() is factory.return_value
    factory.assert_called_once()
    assert factory.call_args.kwargs["max_retries"] == 0
    assert factory.call_args.kwargs["timeout"] == llm_client.LLM_TIMEOUT
