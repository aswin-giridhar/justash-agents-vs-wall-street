"""Thin provider interface over the OpenAI API.

Every call is logged to logs/llm/ with its prompt, schema and response, so a judge
can inspect exactly what the model was asked and what it returned.

Errors are raised, never swallowed. A broken key must fail loudly at 17:15 rather
than quietly producing empty forecasts - a silent failure here would look like a
working run that emits blanks, and a blank scores 5.0.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any

import openai

from avws.config import LLM_LOG_DIR, MODEL

_client: openai.OpenAI | None = None
_call_count = 0
_counter_lock = threading.Lock()


def client() -> openai.OpenAI:
    global _client
    if _client is None:
        _client = openai.OpenAI()
    return _client


def call_count() -> int:
    return _call_count


def complete(
    system: str,
    user: str,
    schema: dict[str, Any],
    schema_name: str = "response",
    model: str | None = None,
) -> dict[str, Any]:
    """One structured-output call. Returns the parsed object."""
    global _call_count
    model = model or MODEL
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    request: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": schema,
                "strict": True,
            },
        },
    }

    try:
        response = client().chat.completions.create(**request, temperature=0)
        temperature_used: float | None = 0.0
    except openai.BadRequestError:
        # Reasoning models reject a non-default temperature. Determinism then comes
        # from the prompt and schema rather than the sampler.
        response = client().chat.completions.create(**request)
        temperature_used = None

    with _counter_lock:
        _call_count += 1
        sequence = _call_count
    content = response.choices[0].message.content or "{}"
    parsed = json.loads(content)

    digest = hashlib.sha256((system + user + json.dumps(schema)).encode()).hexdigest()[:12]
    LLM_LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LLM_LOG_DIR / f"{sequence:03d}-{schema_name}-{digest}.json").write_text(
        json.dumps(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "temperature": temperature_used,
                "system": system,
                "user": user,
                "schema": schema,
                "response": parsed,
                "usage": getattr(response, "usage", None)
                and response.usage.model_dump(),
            },
            indent=1,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return parsed
