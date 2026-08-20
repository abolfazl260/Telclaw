"""Minimal Groq connectivity diagnostic.

This deliberately sends the smallest possible chat-completions request:
model + one user message, with no response_format, schema, temperature,
or application-specific prompt. It is used to isolate provider/model/access
issues from Telclaw's AI extraction payload.
"""

import json
from urllib import request
from urllib.error import HTTPError, URLError

import config


ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


def test_groq_connection():
    api_key = config.GROQ_API_KEY
    model = config.GROQ_MODEL

    if not api_key:
        print("\n[GROQ CONNECTION TEST]")
        print("Result: FAILED")
        print("Reason: missing_api_key")
        return False

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Say OK"}],
    }

    req = request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    print("\n[GROQ CONNECTION TEST]")
    print(f"Model: {model}")
    print(f"Endpoint: {ENDPOINT}")
    print("Payload: minimal chat-completions request (no JSON schema)")

    try:
        with request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            print(f"HTTP Status: {response.status}")
            print(f"Response: {raw[:4000]}")
            print("Result: SUCCESS")
            return True
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        request_id = exc.headers.get("x-request-id") or exc.headers.get("request-id")
        print(f"HTTP Status: {exc.code}")
        print(f"Response: {detail[:4000] or '<empty>'}")
        if request_id:
            print(f"Request ID: {request_id}")
        print("Result: FAILED")
        return False
    except (URLError, TimeoutError) as exc:
        print(f"Transport Error: {exc}")
        print("Result: FAILED")
        return False
