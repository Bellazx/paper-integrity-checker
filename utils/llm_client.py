from __future__ import annotations
import re
import time
import logging
import threading
import os

from openai import APITimeoutError, OpenAI

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from config import (
    MINIMAX_API_KEY, MINIMAX_BASE_URL, MINIMAX_MODEL, MINIMAX_FALLBACK_MODEL,
    LLM_MAX_TOKENS, LLM_TEMPERATURE, LLM_RETRY_ATTEMPTS,
    LLM_TIMEOUT_FALLBACK_THRESHOLD,
)

log = logging.getLogger(__name__)

_client = None
_client_lock = threading.Lock()

LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "300"))


def get_client() -> OpenAI:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = OpenAI(
                    api_key=MINIMAX_API_KEY,
                    base_url=MINIMAX_BASE_URL,
                    timeout=LLM_TIMEOUT,
                    # Count each request here, without hidden SDK retries.
                    max_retries=0,
                )
    return _client


def chat(prompt: str, system: str | None = None, temperature: float | None = None) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system + "\n\n重要：不要使用<think>标签进行推理，直接输出结果。"})
    messages.append({"role": "user", "content": prompt})

    model = MINIMAX_MODEL
    consecutive_timeouts = 0
    retry_failures = 0
    last_error = None
    while True:
        started = time.monotonic()
        deadline = float(os.environ.get("PAPER_LLM_DEADLINE", "0")) or None
        remaining = deadline - started if deadline is not None else LLM_TIMEOUT
        if remaining <= 0:
            raise TimeoutError("Report model-call budget exhausted") from last_error
        try:
            resp = get_client().chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature or LLM_TEMPERATURE,
                max_tokens=LLM_MAX_TOKENS,
                reasoning_effort=os.getenv('LLM_REASONING_EFFORT', 'low'),
                timeout=min(LLM_TIMEOUT, remaining),
            )
            content = resp.choices[0].message.content or ""
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            if not content:
                raise RuntimeError("LLM exhausted tokens on reasoning, no useful output")
            return content
        except Exception as e:
            last_error = e
            if isinstance(e, (APITimeoutError, TimeoutError)) and model != MINIMAX_FALLBACK_MODEL:
                # Primary timeouts have a separate budget so five can trigger fallback.
                consecutive_timeouts += 1
                log.warning("LLM model=%s timed out (%d/%d consecutive); endpoint=%s/chat/completions "
                            "elapsed=%.1fs timeout=%ss error=%s cause=%s prompt_chars=%d",
                            model, consecutive_timeouts, LLM_TIMEOUT_FALLBACK_THRESHOLD,
                            MINIMAX_BASE_URL.rstrip('/'), time.monotonic() - started, LLM_TIMEOUT,
                            type(e).__name__, type(e.__cause__).__name__ if e.__cause__ else 'none',
                            sum(len(m['content']) for m in messages))
                if consecutive_timeouts >= LLM_TIMEOUT_FALLBACK_THRESHOLD:
                    log.warning("Switching LLM from %s to %s after %d consecutive timeouts",
                                model, MINIMAX_FALLBACK_MODEL, consecutive_timeouts)
                    model = MINIMAX_FALLBACK_MODEL
                    consecutive_timeouts = 0
                    retry_failures = 0
                    continue
                backoff = consecutive_timeouts - 1
            else:
                consecutive_timeouts = 0
                retry_failures += 1
                log.warning("LLM model=%s call failed (attempt %d/%d): %s",
                            model, retry_failures, LLM_RETRY_ATTEMPTS, e)
                if retry_failures >= LLM_RETRY_ATTEMPTS:
                    break
                backoff = retry_failures - 1
            delay = 2 ** min(backoff, 4)
            if deadline is not None and time.monotonic() + delay >= deadline:
                raise TimeoutError("Report model-call budget exhausted") from e
            time.sleep(delay)

    raise RuntimeError(f"LLM call failed after all retries (model={model})") from last_error
