"""Groq connectivity diagnostics using Python urllib and system curl."""

import json
import os
import shutil
import subprocess
from urllib import request
from urllib.error import HTTPError, URLError

import config


ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"


def _payload(model):
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Say OK"}],
    }


def _print_header():
    print("\n[GROQ CONNECTION TEST]")
    print(f"Model: {config.GROQ_MODEL}")
    print(f"Endpoint: {ENDPOINT}")
    print("Payload: minimal chat-completions request (no JSON schema)")


def test_python_urllib():
    api_key = config.GROQ_API_KEY
    if not api_key:
        print("\n[1] Python urllib")
        print("Result: FAILED")
        print("Reason: missing_api_key")
        return False

    req = request.Request(
        ENDPOINT,
        data=json.dumps(_payload(config.GROQ_MODEL)).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    print("\n[1] Python urllib")
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


def test_system_curl():
    print("\n[2] System curl")
    curl = shutil.which("curl")
    api_key = config.GROQ_API_KEY
    if not curl:
        print("Result: FAILED")
        print("Reason: curl_not_found")
        return False
    if not api_key:
        print("Result: FAILED")
        print("Reason: missing_api_key")
        return False

    payload = json.dumps(_payload(config.GROQ_MODEL))
    env = os.environ.copy()
    # Pass the secret through the process environment instead of putting it
    # in argv, so it cannot appear in shell history or process arguments.
    env["TELCLAW_GROQ_TEST_KEY"] = api_key
    command = [
        curl,
        "-sS",
        "-i",
        "--connect-timeout",
        "10",
        "--max-time",
        "30",
        ENDPOINT,
        "-H",
        "Authorization: Bearer ${TELCLAW_GROQ_TEST_KEY}",
        "-H",
        "Content-Type: application/json",
        "--data-raw",
        payload,
    ]

    # shell=True is used only so the environment variable can be expanded;
    # no user input is interpolated into the command string.
    command_text = " ".join(
        subprocess.list2cmdline([part]) for part in command
    )
    command_text = command_text.replace(
        "\"Authorization: Bearer ${TELCLAW_GROQ_TEST_KEY}\"",
        '"Authorization: Bearer $TELCLAW_GROQ_TEST_KEY"',
    )
    try:
        completed = subprocess.run(
            command_text,
            shell=True,
            env=env,
            capture_output=True,
            text=True,
            timeout=35,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"Transport Error: {exc}")
        print("Result: FAILED")
        return False

    output = (completed.stdout or "") + (completed.stderr or "")
    print(output[:6000])
    if completed.returncode == 0:
        # curl exits 0 for HTTP 4xx/5xx, so inspect the HTTP status separately.
        status_lines = [
            line for line in output.splitlines() if line.startswith("HTTP/")
        ]
        success = any(" 200 " in line or " 201 " in line for line in status_lines)
    else:
        success = False
    print("Result: SUCCESS" if success else "Result: FAILED")
    return success


def test_groq_connection():
    _print_header()
    python_ok = test_python_urllib()
    curl_ok = test_system_curl()
    print("\n[COMPARISON]")
    print(f"Python urllib: {'SUCCESS' if python_ok else 'FAILED'}")
    print(f"System curl:   {'SUCCESS' if curl_ok else 'FAILED'}")
    return python_ok and curl_ok
