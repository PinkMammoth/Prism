"""HTTP client shared by providers.

Responsibilities:
- rate limiting per provider,
- bounded retries with exponential backoff on transient errors,
- capturing every raw response body so the ingestion layer can archive it with a
  SHA-256 hash (provenance),
- clear errors: network policy/DNS failures surface as ``ProviderUnavailable``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from market_signal.models.domain import utcnow


class ProviderError(RuntimeError):
    """Provider returned something we cannot use (bad status, schema change...)."""


class ProviderUnavailable(ProviderError):
    """Provider unreachable (network, DNS, proxy denial, persistent 5xx)."""


class SchemaError(ProviderError):
    """Provider response does not match the expected schema. Never guess — fail."""


@dataclass
class RawPayload:
    provider: str
    method: str
    url: str
    params: dict[str, Any]
    status: int
    body: bytes
    fetched_at: datetime


@dataclass
class HttpClient:
    provider: str
    base_url: str
    requests_per_second: float = 1.0
    timeout_seconds: float = 30.0
    max_retries: int = 4
    backoff_seconds: float = 2.0
    user_agent: str = "Prism/0.1"
    headers: dict[str, str] = field(default_factory=dict)
    captured: list[RawPayload] = field(default_factory=list)
    transport: httpx.BaseTransport | None = None  # injectable for tests
    _last_request: float = 0.0
    _client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                headers={"User-Agent": self.user_agent, **self.headers},
                transport=self.transport,
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _throttle(self) -> None:
        if self.requests_per_second <= 0:
            return
        min_gap = 1.0 / self.requests_per_second
        wait = self._last_request + min_gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        redact_params: tuple[str, ...] = (),
    ) -> bytes:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self._http().request(method, path, params=params, json=json_body)
            except (httpx.ConnectError, httpx.ProxyError, httpx.ConnectTimeout) as exc:
                # Network policy or DNS failures are not transient in practice: fail fast.
                raise ProviderUnavailable(f"{self.provider}: cannot connect ({exc})") from exc
            except httpx.TransportError as exc:
                last_exc = exc
                time.sleep(self.backoff_seconds * (2**attempt))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = ProviderError(f"{self.provider}: HTTP {resp.status_code}")
                time.sleep(self.backoff_seconds * (2**attempt))
                continue
            safe_params = {k: ("***" if k in redact_params else v) for k, v in params.items()}
            if json_body is not None:
                safe_params = {**safe_params, "_json": json_body}
            self.captured.append(
                RawPayload(
                    provider=self.provider,
                    method=method,
                    url=str(resp.request.url.copy_with(query=None)),
                    params=safe_params,
                    status=resp.status_code,
                    body=resp.content,
                    fetched_at=utcnow(),
                )
            )
            if resp.status_code >= 400:
                raise ProviderError(
                    f"{self.provider}: HTTP {resp.status_code} for {path}: {resp.text[:300]}"
                )
            return resp.content
        raise ProviderUnavailable(f"{self.provider}: retries exhausted ({last_exc})")

    def get_json(self, path: str, **kw: Any) -> Any:
        import json

        body = self.request("GET", path, **kw)
        try:
            return json.loads(body)
        except ValueError as exc:
            raise SchemaError(f"{self.provider}: non-JSON response from {path}") from exc

    def post_json(self, path: str, payload: Any) -> Any:
        import json

        body = self.request("POST", path, json_body=payload)
        try:
            return json.loads(body)
        except ValueError as exc:
            raise SchemaError(f"{self.provider}: non-JSON response from {path}") from exc

    def drain(self) -> list[RawPayload]:
        out, self.captured = self.captured, []
        return out
