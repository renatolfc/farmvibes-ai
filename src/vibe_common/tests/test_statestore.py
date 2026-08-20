# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock

import pytest

from vibe_common.statestore import StateStore, StateStoreConflictError


class MockResponse:
    def __init__(
        self,
        content: Any,
        headers: Optional[Dict[str, str]] = None,
        ok: bool = True,
        status: int = 200,
    ):
        self._content = content
        self.headers = headers or {}
        self.ok = ok
        self.status = status

    async def json(self, loads: Any, **kwargs: Any) -> Any:
        return loads(self._content, **kwargs)

    async def text(self) -> str:
        return str(self._content)


@pytest.mark.anyio
async def test_store_fails_with_invalid_input():
    store = StateStore()
    for value in [float(x) for x in "inf -inf nan".split()]:
        with pytest.raises(ValueError):
            await store.store("key", value)


@pytest.mark.anyio
async def test_retrieve_returns_etag():
    store = StateStore()
    store.vibe_dapr_client.get = AsyncMock(
        return_value=MockResponse(json.dumps({"value": 1}), {"ETag": "7"})
    )

    value, etag = await store.retrieve_with_etag("key")

    assert value == {"value": 1}
    assert etag == "7"


@pytest.mark.anyio
async def test_delete_uses_transaction_delete_operation():
    store = StateStore()
    store.vibe_dapr_client.post = AsyncMock(return_value=MockResponse(""))

    await store.delete("key")

    request = store.vibe_dapr_client.post.call_args.kwargs["data"]
    assert request["operations"] == [{"operation": "delete", "request": {"key": "key"}}]


@pytest.mark.anyio
async def test_transaction_reports_etag_conflict():
    store = StateStore()
    store.vibe_dapr_client.post = AsyncMock(
        return_value=MockResponse("possible etag mismatch", ok=False, status=500)
    )

    with pytest.raises(StateStoreConflictError):
        await store.transaction(
            [
                {
                    "key": "runs",
                    "operation": "upsert",
                    "value": [],
                    "etag": "1",
                    "options": {"concurrency": "first-write"},
                }
            ]
        )
