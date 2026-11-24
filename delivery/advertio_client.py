"""HTTP client for the Advertio Ingest API contract (ADR-0009)."""

import math
import re
from pathlib import Path
from urllib.parse import quote
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from delivery.advertio_transport import record_cooldown, request_slot

import requests


class AdvertioError(RuntimeError):
    def __init__(
        self,
        message,
        *,
        status=None,
        retryable=False,
        already_existed=False,
        lead_id=None,
        retry_after=None,
        deferred=False,
    ):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.already_existed = already_existed
        self.lead_id = lead_id
        self.retry_after = retry_after
        self.deferred = deferred


class AdvertioClient:
    """Small transport-only client. Business mapping stays in AdvertioDeliveryService."""

    def __init__(self, base_url, ingest_key, timeout=60, category="legacy"):
        self.base_url = base_url.rstrip("/")
        self.ingest_key = ingest_key
        self.timeout = timeout
        self.category = category

    @property
    def headers(self):
        return {"X-Ingest-Key": self.ingest_key}

    @staticmethod
    def _retry_after(response):
        raw = (getattr(response, "headers", None) or {}).get("Retry-After")
        if raw:
            try:
                delay = float(raw)
            except (TypeError, ValueError):
                try:
                    delay = (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    return None
            if math.isfinite(delay):
                return min(30 * 86400, max(0, delay))
        return None

    def _raise_for_response(self, response):
        status = response.status_code
        if status < 300:
            return
        raise AdvertioError(
            f"Advertio request failed: HTTP {status}",
            status=status,
            retryable=status in {401, 404, 429} or status >= 500,
            retry_after=self._retry_after(response),
        )

    def _request(self, method, url, *, bucket=None, accept_missing=False, on_admit=None, **kwargs):
        with request_slot(bucket or self.category, self.timeout, on_admit=on_admit):
            # Custom X-Ingest-Key headers must never follow redirects.
            response = getattr(requests, method)(url, allow_redirects=False, **kwargs)
            status = response.status_code
            if status in {401, 404, 429} and not (
                accept_missing and status == 404 and self._confirmed_absent(response)
            ):
                record_cooldown(status, self._retry_after(response))
            return response

    def catalog(self, path, **params):
        try:
            response = self._request(
                "get",
                f"{self.base_url}/api/{path}",
                bucket="catalog",
                headers=self.headers,
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AdvertioError("catalog_transport_error", retryable=True) from exc
        self._raise_for_response(response)
        try:
            body = response.json()
        except ValueError as exc:
            raise AdvertioError("invalid_catalog_json", retryable=True) from exc
        if not isinstance(body, list):
            raise AdvertioError("invalid_catalog_shape", retryable=True)
        return body

    def upload_media(self, path, source_name):
        file_path = Path(path)
        if not file_path.is_file():
            raise AdvertioError(f"Advertio media file does not exist: {path}")
        if file_path.stat().st_size > 8 * 1024 * 1024:
            raise AdvertioError("Advertio media file exceeds the 8 MB limit")

        try:
            with file_path.open("rb") as handle:
                response = self._request(
                    "post",
                    f"{self.base_url}/api/ingest/media",
                    params={"source": source_name},
                    headers=self.headers,
                    files={"file": (file_path.name, handle)},
                    timeout=self.timeout,
                )
        except requests.RequestException as exc:
            raise AdvertioError(
                "Advertio media transport error", retryable=True
            ) from exc

        self._raise_for_response(response)
        try:
            payload = response.json()
            key = payload["key"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AdvertioError(
                "Advertio media response did not contain a valid key"
            ) from exc
        return key

    def list_cities(self, country_code):
        return self.catalog("locations/cities", countryCode=country_code, limit=200)

    def create_lead(self, payload, *, on_admit=None):
        try:
            response = self._request(
                "post",
                f"{self.base_url}/api/ingest/leads",
                headers={**self.headers, "Content-Type": "application/json"},
                json=payload,
                on_admit=on_admit,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AdvertioError(
                "Advertio lead transport error", retryable=True
            ) from exc

        if response.status_code not in {200, 201}:
            self._raise_for_response(response)
            raise AdvertioError(
                "Unexpected Advertio lead HTTP status",
                status=response.status_code,
                retryable=True,
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise AdvertioError(
                "Advertio lead response was not valid JSON",
                status=response.status_code,
                retryable=True,
            ) from exc

        if not isinstance(body, dict) or not body.get("leadId"):
            raise AdvertioError(
                "Advertio lead response did not contain leadId",
                status=response.status_code,
                retryable=True,
            )

        if self.category == "jobs" and (
            not isinstance(body.get("alreadyExisted"), bool)
            or not isinstance(body.get("status"), str)
            or not body["status"].strip()
            or (response.status_code == 200 and not body["alreadyExisted"])
        ):
            raise AdvertioError(
                "Invalid Jobs ingest success contract",
                status=response.status_code,
                retryable=True,
            )

        return {
            "lead_id": body["leadId"],
            "status": body.get("status"),
            "already_existed": bool(body.get("alreadyExisted")),
            "http_status": response.status_code,
        }

    @staticmethod
    def _confirmed_absent(response):
        # An empty/proxy 404 can also mean the ingest route is not mapped.
        # Only explicit listing-level evidence settles the deletion.
        try:
            body = response.json()
        except ValueError:
            return False
        if isinstance(body, dict):
            body = body.get("code") or body.get("title") or body.get("message")
        return isinstance(body, str) and body.strip().casefold().rstrip(".") in {
            "lead_not_found", "listing_not_found", "lead not found", "listing not found",
        }

    def delete_lead(self, source_name, external_id):
        key = f"{quote(str(source_name), safe='')}/{quote(str(external_id), safe='')}"
        try:
            response = self._request(
                "delete",
                f"{self.base_url}/api/ingest/leads/{key}",
                headers=self.headers, timeout=self.timeout,
                bucket="deletion", accept_missing=True,
            )
        except requests.RequestException as exc:
            raise AdvertioError("Advertio delete transport error", retryable=True) from exc
        if response.status_code == 204 or (
            response.status_code == 404 and self._confirmed_absent(response)
        ):
            return response.status_code
        self._raise_for_response(response)
        raise AdvertioError("Unexpected Advertio delete HTTP status", retryable=True)

    def deactivate_source(self, source_name, *, confirm_source):
        if confirm_source != source_name or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,31}", source_name):
            raise AdvertioError("Confirm the exact Advertio source before deactivation")
        try:
            response = self._request(
                "delete",
                f"{self.base_url}/api/ingest/sources/{source_name}",
                headers=self.headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise AdvertioError(
                "Advertio source deactivation transport error", retryable=True
            ) from exc
        self._raise_for_response(response)
        try:
            return response.json()
        except ValueError as exc:
            raise AdvertioError(
                "Advertio source deactivation response was not valid JSON"
            ) from exc
