import re
import time
import logging
import threading
import os

from openai import OpenAI

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from config import (
    MINIMAX_API_KEY, MINIMAX_BASE_URL, MINIMAX_MODEL,
    LLM_MAX_TOKENS, LLM_TEMPERATURE, LLM_RETRY_ATTEMPTS,
)

log = logging.getLogger(__name__)

_client = None
_client_lock = threading.Lock()

LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "180"))


def get_client() -> OpenAI:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = OpenAI(
                    api_key=MINIMAX_API_KEY,
                    base_url=MINIMAX_BASE_URL,
                    timeout=LLM_TIMEOUT,
                )
    return _client


def chat(prompt: str, system: str | None = None, temperature: float | None = None) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system + "\n\n重要：不要使用<think>标签进行推理，直接输出结果。"})
    messages.append({"role": "user", "content": prompt})

    for attempt in range(LLM_RETRY_ATTEMPTS):
        try:
            resp = get_client().chat.completions.create(
                model=MINIMAX_MODEL,
                messages=messages,
                temperature=temperature or LLM_TEMPERATURE,
                max_tokens=LLM_MAX_TOKENS,
            )
            content = resp.choices[0].message.content
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            if not content:
                log.warning("LLM returned empty content after stripping <think> tags (attempt %d/%d)", attempt + 1, LLM_RETRY_ATTEMPTS)
                if attempt < LLM_RETRY_ATTEMPTS - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError("LLM exhausted tokens on reasoning, no useful output")
            return content
        except RuntimeError:
            raise
        except Exception as e:
            log.warning("LLM call failed (attempt %d/%d): %s", attempt + 1, LLM_RETRY_ATTEMPTS, e)
            if attempt < LLM_RETRY_ATTEMPTS - 1:
                time.sleep(2 ** attempt)

    raise RuntimeError("LLM call failed after all retries")
