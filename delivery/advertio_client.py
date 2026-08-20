"""HTTP client for the Advertio Ingest API contract (ADR-0009)."""

from pathlib import Path

import requests


class AdvertioError(RuntimeError):
    def __init__(self, message, *, status=None, retryable=False, already_existed=False, lead_id=None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.already_existed = already_existed
        self.lead_id = lead_id


class AdvertioClient:
    """Small transport-only client. Business mapping stays in AdvertioDeliveryService."""

    def __init__(self, base_url, ingest_key, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.ingest_key = ingest_key
        self.timeout = timeout

    @property
    def headers(self):
        return {"X-Ingest-Key": self.ingest_key}

    def _raise_for_response(self, response):
        if response.status_code < 400:
            return
        detail = response.text.strip()[:4000]
        retryable = response.status_code >= 500
        raise AdvertioError(
            f"Advertio request failed: HTTP {response.status_code}: {detail}",
            status=response.status_code,
            retryable=retryable,
        )

    def upload_media(self, path, source_name):
        file_path = Path(path)
        if not file_path.is_file():
            raise AdvertioError(f"Advertio media file does not exist: {path}")
        if file_path.stat().st_size > 8 * 1024 * 1024:
            raise AdvertioError("Advertio media file exceeds the 8 MB limit")

        try:
            with file_path.open("rb") as handle:
                response = requests.post(
                    f"{self.base_url}/api/ingest/media",
                    params={"source": source_name},
                    headers=self.headers,
                    files={"file": (file_path.name, handle)},
                    timeout=self.timeout,
                )
        except requests.RequestException as exc:
            raise AdvertioError(f"Advertio media transport error: {exc}", retryable=True) from exc

        self._raise_for_response(response)
        try:
            payload = response.json()
            key = payload["key"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AdvertioError("Advertio media response did not contain a valid key") from exc
        return key

    def create_lead(self, payload):
        try:
            response = requests.post(
                f"{self.base_url}/api/ingest/leads",
                headers={**self.headers, "Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AdvertioError(f"Advertio lead transport error: {exc}", retryable=True) from exc

        if response.status_code not in {200, 201}:
            self._raise_for_response(response)

        try:
            body = response.json()
        except ValueError as exc:
            raise AdvertioError("Advertio lead response was not valid JSON", status=response.status_code, retryable=response.status_code >= 500) from exc

        if not isinstance(body, dict) or not body.get("leadId"):
            raise AdvertioError("Advertio lead response did not contain leadId", status=response.status_code)

        return {
            "lead_id": body["leadId"],
            "status": body.get("status"),
            "already_existed": bool(body.get("alreadyExisted")),
            "http_status": response.status_code,
        }

    def delete_lead(self, source_name, external_id):
        try:
            response = requests.delete(
                f"{self.base_url}/api/ingest/leads/{source_name}/{external_id}",
                headers=self.headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AdvertioError(f"Advertio delete transport error: {exc}", retryable=True) from exc
        if response.status_code not in {204, 404}:
            self._raise_for_response(response)
        return response.status_code

    def deactivate_source(self, source_name):
        try:
            response = requests.delete(
                f"{self.base_url}/api/ingest/sources/{source_name}",
                headers=self.headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AdvertioError(f"Advertio source deactivation transport error: {exc}", retryable=True) from exc
        self._raise_for_response(response)
        try:
            return response.json()
        except ValueError as exc:
            raise AdvertioError("Advertio source deactivation response was not valid JSON") from exc
