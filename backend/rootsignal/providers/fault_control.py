"""ChangeProvider backed by the demo's fault-control change log (GET /changes).

In a real deployment this would be GitHub deployments, Argo CD, or a CI/CD audit log.
"""

from datetime import datetime

import httpx
from pydantic import ValidationError

from rootsignal.providers.changes import Change, ChangeProvider, ChangeProviderError


class FaultControlChangeProvider(ChangeProvider):
    name = "fault_control"

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None):
        self._url = base_url.rstrip("/") + "/changes"
        self._client = client or httpx.AsyncClient(timeout=5.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def recent_changes(self, service: str | None, since: datetime) -> list[Change]:
        params: dict[str, str | int] = {"since": since.isoformat(), "limit": 200}
        if service:
            params["service"] = service
        try:
            resp = await self._client.get(self._url, params=params)
            resp.raise_for_status()
            return [Change.model_validate(c) for c in resp.json()]
        except (httpx.HTTPError, ValueError, ValidationError) as exc:
            raise ChangeProviderError(f"{self._url}: {exc!r}") from exc
