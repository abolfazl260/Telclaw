"""AI extraction service using the OpenAI Responses API."""

import json
from urllib import request
from urllib.error import HTTPError, URLError

import config
from ai.category_schemas import build_json_schema, validate_result


SYSTEM_PROMPT = """You extract structured marketplace information from processed Telegram messages.
Classify each message into exactly one of: housinglist, transferlist, joblist.
Extract only facts explicitly supported by the message. Never invent values.
Return null for an unknown scalar field and [] for an unknown list field.
Use normalized English field names. Keep original meaning and do not copy Telegram metadata.
"""


class AIExtractionError(RuntimeError):
    pass


class OpenAIExtractor:
    """Small dependency-free OpenAI client; keeps provider logic isolated from storage."""

    def __init__(self, api_key=None, model=None, timeout=60):
        self.api_key = api_key or config.OPENAI_API_KEY
        self.model = model or config.OPENAI_MODEL
        self.timeout = timeout

    def extract(self, processed_text):
        if not self.api_key:
            raise AIExtractionError("OPENAI_API_KEY is not configured")

        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_PROMPT}]},
                {"role": "user", "content": [{"type": "input_text", "text": processed_text}]},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "telclaw_category_extraction",
                    "strict": False,
                    "schema": build_json_schema(),
                }
            },
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            "https://api.openai.com/v1/responses",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise AIExtractionError(f"OpenAI request failed: {exc}") from exc

        try:
            response_data = json.loads(raw)
            output_text = response_data.get("output_text")
            if not output_text:
                for item in response_data.get("output", []):
                    for content in item.get("content", []):
                        if content.get("type") in {"output_text", "text"} and content.get("text"):
                            output_text = content["text"]
                            break
                    if output_text:
                        break
            if not output_text:
                raise ValueError("No structured output returned")
            result = json.loads(output_text)
            return validate_result(result)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AIExtractionError(f"Invalid AI output: {exc}") from exc
