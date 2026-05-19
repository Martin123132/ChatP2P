"""Small Ollama HTTP client for local inference jobs."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"


class OllamaError(RuntimeError):
    """Raised when a local Ollama inference request cannot produce an answer."""


def generate_ollama(
    *,
    model: str,
    prompt: str,
    temperature: float | None = None,
    base_url: str = DEFAULT_OLLAMA_BASE_URL,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
    }
    if temperature is not None:
        payload["options"] = {"temperature": temperature}

    request = Request(
        f"{base_url.rstrip('/')}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise OllamaError(f"Ollama request failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise OllamaError(f"Ollama is not reachable at {base_url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise OllamaError(f"Ollama request timed out after {timeout_seconds}s") from exc

    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise OllamaError("Ollama returned invalid JSON") from exc

    answer = data.get("response")
    if not isinstance(answer, str):
        raise OllamaError("Ollama response did not include a string response field")

    return {
        "answer": answer,
        "model": data.get("model", model),
        "confidence": 1.0,
        "done": bool(data.get("done", False)),
        "ollama": {
            "total_duration": data.get("total_duration"),
            "load_duration": data.get("load_duration"),
            "prompt_eval_count": data.get("prompt_eval_count"),
            "eval_count": data.get("eval_count"),
            "eval_duration": data.get("eval_duration"),
        },
    }
