"""AI extraction service using Groq structured outputs."""

import json
import logging
from urllib import request
from urllib.error import HTTPError, URLError

import config
from ai.category_schemas import build_json_schema, validate_result
from ai.rate_limiter import RateLimiter


SYSTEM_PROMPT = """You extract structured marketplace information from processed Telegram messages.
Classify each message into exactly one of: housinglist, transferlist, joblist.
Extract only facts explicitly supported by the message. Never invent values.
Return null for an unknown scalar field and [] for an unknown list field.
Use normalized English field names. Keep original meaning and do not copy Telegram metadata.
Return ONLY the JSON object required by the supplied schema.
"""


logger = logging.getLogger("telclaw.ai")


class AIExtractionError(RuntimeError):
    """An AI failure whose details are diagnostic-only and must not reach message storage."""

    def __init__(self, message, *, status=None, reason=None, provider="groq", stop_queue=False):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.provider = provider
        self.stop_queue = stop_queue


class GroqExtractor:
    """Dependency-free Groq client using Groq's OpenAI-compatible API."""

    ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key=None, model=None, timeout=60, rate_limiter=None):
        self.api_key = api_key or config.GROQ_API_KEY
        self.model = model or config.GROQ_MODEL
        self.timeout = timeout
        self.rate_limiter = rate_limiter or RateLimiter(
            requests_per_minute=config.GROQ_REQUESTS_PER_MINUTE
        )

    @staticmethod
    def _provider_reason(status, detail):
        """Normalize provider responses to a small, non-persisted diagnostic reason."""
        text = (detail or "").lower()
        if status == 401:
            return "invalid_api_key"
        if status == 403:
            if "model" in text and ("block" in text or "permission" in text or "access" in text):
                return "model_blocked"
            return "permissions_error"
        if status == 429:
            return "rate_limit"
        if status >= 500:
            return "server_error"
        return "provider_error"

    def _log_request_config(self):
        """Log request configuration without exposing any part of the API secret."""
        logger.warning(
            "[DEBUG GROQ REQUEST] MODEL=%s ENDPOINT=%s",
            self.model,
            self.ENDPOINT,
        )
        print(
            "\n[DEBUG GROQ REQUEST]\n"
            f"MODEL:\n{self.model}\n"
            f"ENDPOINT:\n{self.ENDPOINT}\n"
        )

    @staticmethod
    def _log_provider_error(status, model, reason, detail, request_id=None):
        """Print the provider's diagnostic response without exposing credentials."""
        response_detail = detail or "<empty>"
        diagnostic = (
            "\n[AI PROVIDER ERROR]\n"
            "Provider: Groq\n"
            f"HTTP Status: {status}\n"
            f"Model: {model}\n"
            f"Reason: {reason}\n"
            f"Response: {response_detail[:4000]}\n"
        )
        if request_id:
            diagnostic += f"Request ID: {request_id}\n"
        print(diagnostic)
        logger.error(
            "Groq provider error: status=%s model=%s reason=%s request_id=%s response=%s",
            status,
            model,
            reason,
            request_id or "<none>",
            response_detail[:4000],
        )

    def extract(self, processed_text):
        if not self.api_key:
            raise AIExtractionError(
                "GROQ_API_KEY is not configured",
                reason="missing_api_key",
            )
        if not isinstance(processed_text, str) or not processed_text.strip():
            raise AIExtractionError("Cannot call Groq with empty text", reason="invalid_input")

        self.rate_limiter.wait()
        self._log_request_config()

        # Diagnostic mode: send ordinary JSON output without response_format/json_schema.
        # This isolates Groq access/model permissions from Structured Output support.
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": processed_text},
            ],
            "temperature": 0,
        }

        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.ENDPOINT,
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
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            request_id = exc.headers.get("x-request-id") or exc.headers.get("request-id")
            reason = self._provider_reason(exc.code, detail)
            self._log_provider_error(
                exc.code,
                self.model,
                reason,
                detail,
                request_id=request_id,
            )
            diagnostic = [
                "provider=groq",
                f"status={exc.code}",
                f"model={self.model}",
                f"reason={reason}",
                f"response={detail[:4000] or '<empty>'}",
            ]
            if request_id:
                diagnostic.append(f"request_id={request_id}")
            raise AIExtractionError(
                "Groq request failed: " + "; ".join(diagnostic),
                status=exc.code,
                reason=reason,
                stop_queue=exc.code == 403,
            ) from exc
        except (URLError, TimeoutError) as exc:
            raise AIExtractionError(
                f"Groq request failed: provider=groq; model={self.model}; transport={exc}",
                reason="network_error" if isinstance(exc, URLError) else "timeout",
            ) from exc

        try:
            response_data = json.loads(raw)
            choices = response_data.get("choices", [])
            if not choices:
                raise ValueError("No choices returned")
            message = choices[0].get("message", {})
            if message.get("refusal"):
                raise ValueError(f"Model refused extraction: {message['refusal']}")
            output_text = message.get("content")
            if not output_text:
                raise ValueError("No structured output returned")
            result = json.loads(output_text)
            return validate_result(result)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AIExtractionError(
                f"Invalid Groq output: {exc}",
                reason="invalid_provider_output",
            ) from exc
