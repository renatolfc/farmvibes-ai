#!/usr/bin/env python
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

# -*- coding: utf-8 -*-

import asyncio
import logging
from typing import Any, Dict, List, Optional, Protocol, Tuple

from typing_extensions import NotRequired, Required, TypedDict

from vibe_common.constants import STATE_URL_TEMPLATE
from vibe_common.vibe_dapr_client import VibeDaprClient

LOGGER = logging.getLogger(__name__)
STATE_STORE = "statestore"
METADATA = {"partitionKey": "eywa"}
DEFAULT_BULK_PARALLELISM = 8


class TransactionOperation(TypedDict):
    key: Required[str]
    operation: Required[str]
    value: NotRequired[Optional[Any]]
    etag: NotRequired[str]
    options: NotRequired[Dict[str, str]]


class StateStoreConflictError(RuntimeError):
    """Raised when an optimistic state-store write loses a race."""


class StateStoreProtocol(Protocol):
    async def retrieve(self, key: str, traceparent: Optional[str] = None) -> Any: ...

    async def retrieve_with_etag(
        self,
        key: str,
        traceparent: Optional[str] = None,
        consistency: Optional[str] = None,
    ) -> Tuple[Any, Optional[str]]: ...

    async def retrieve_bulk(
        self,
        keys: List[str],
        parallelism: int = DEFAULT_BULK_PARALLELISM,
        traceparent: Optional[str] = None,
    ) -> List[Any]: ...

    async def retrieve_bulk_existing(self, keys: List[str]) -> Dict[str, Any]: ...

    async def store(self, key: str, obj: Any, traceparent: Optional[str] = None) -> None: ...

    async def store_if_absent(
        self, key: str, obj: Any, traceparent: Optional[str] = None
    ) -> None: ...

    async def delete(self, key: str, traceparent: Optional[str] = None) -> None: ...

    async def transaction(
        self, operations: List[TransactionOperation], traceparent: Optional[str] = None
    ) -> None: ...


class StateStore(StateStoreProtocol):
    def __init__(
        self,
        state_store: str = STATE_STORE,
        partition_key: str = METADATA["partitionKey"],
    ):
        self.vibe_dapr_client = VibeDaprClient()
        self.state_store: str = state_store
        self.partition_key: str = partition_key
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    async def retrieve(self, key: str, traceparent: Optional[str] = None) -> Any:
        value, _ = await self.retrieve_with_etag(key, traceparent)
        return value

    async def retrieve_with_etag(
        self,
        key: str,
        traceparent: Optional[str] = None,
        consistency: Optional[str] = None,
    ) -> Tuple[Any, Optional[str]]:
        try:
            params = {"metadata.partitionKey": self.partition_key}
            if consistency is not None:
                params["consistency"] = consistency
            response = await self.vibe_dapr_client.get(
                STATE_URL_TEMPLATE.format(self.state_store, key),
                traceparent=traceparent,
                params=params,
            )

            return (
                await self.vibe_dapr_client.response_json(response),
                response.headers.get("ETag"),
            )
        except KeyError as e:
            raise KeyError(f"Key {key} not found") from e

    async def retrieve_bulk(
        self,
        keys: List[str],
        parallelism: int = DEFAULT_BULK_PARALLELISM,
        traceparent: Optional[str] = None,
    ) -> List[Any]:
        """Retrieves keys in bulk.

        This only exists because our UI needs to display details about all
        workflows, and retrieving in bulk saves on round trips to the state
        store.
        """

        response = await self.vibe_dapr_client.post(
            url=STATE_URL_TEMPLATE.format(self.state_store, "bulk"),
            data={
                "keys": keys,
                "parallelism": parallelism,
            },
            traceparent=traceparent,
            params={"metadata.partitionKey": self.partition_key},
        )

        states = await self.vibe_dapr_client.response_json(response)

        state_by_key = {
            state["key"]: state.get("data")
            for state in states
            if isinstance(state, dict) and "key" in state
        }
        missing = {key for key in keys if state_by_key.get(key) is None}
        if missing:
            raise KeyError(f"Failed to retrieve keys {missing} from state store.")
        return [state_by_key[key] for key in keys]

    async def retrieve_bulk_existing(self, keys: List[str]) -> Dict[str, Any]:
        if not keys:
            return {}
        try:
            values = await self.retrieve_bulk(keys)
            if len(values) == len(keys):
                return {key: value for key, value in zip(keys, values) if value is not None}
        except KeyError:
            pass

        async def retrieve_one(key: str) -> Tuple[str, Optional[Any]]:
            try:
                return key, await self.retrieve(key)
            except KeyError:
                return key, None

        existing: Dict[str, Any] = {}
        for start in range(0, len(keys), DEFAULT_BULK_PARALLELISM):
            chunk = keys[start : start + DEFAULT_BULK_PARALLELISM]
            for key, value in await asyncio.gather(*(retrieve_one(key) for key in chunk)):
                if value is not None:
                    existing[key] = value
        return existing

    async def store(self, key: str, obj: Any, traceparent: Optional[str] = None) -> None:
        response = await self.vibe_dapr_client.post(
            STATE_URL_TEMPLATE.format(self.state_store, ""),
            data=[
                {
                    "key": key,
                    "value": self.vibe_dapr_client.obj_json(obj),
                    "metadata": {"partitionKey": self.partition_key},
                }
            ],
            traceparent=traceparent,
        )
        assert response.ok, "Failed to store state, but underlying method didn't capture it"

    async def store_if_absent(
        self, key: str, obj: Any, traceparent: Optional[str] = None
    ) -> None:
        response = await self.vibe_dapr_client.post(
            STATE_URL_TEMPLATE.format(self.state_store, ""),
            data=[
                {
                    "key": key,
                    "value": self.vibe_dapr_client.obj_json(obj),
                    "options": {
                        "concurrency": "first-write",
                        "consistency": "strong",
                    },
                    "metadata": {"partitionKey": self.partition_key},
                }
            ],
            traceparent=traceparent,
        )
        if not response.ok:
            raise StateStoreConflictError(await response.text())

    async def delete(self, key: str, traceparent: Optional[str] = None) -> None:
        await self.transaction(
            [TransactionOperation(key=key, operation="delete")],
            traceparent=traceparent,
        )

    async def transaction(
        self, operations: List[TransactionOperation], traceparent: Optional[str] = None
    ) -> None:
        queries = []
        for operation in operations:
            request: Dict[str, Any] = {"key": operation["key"]}
            if operation["operation"] != "delete":
                request["value"] = self.vibe_dapr_client.obj_json(operation.get("value"))
            if "etag" in operation:
                request["etag"] = operation["etag"]
            if "options" in operation:
                request["options"] = operation["options"]
            queries.append({"operation": operation["operation"], "request": request})

        response = await self.vibe_dapr_client.post(
            url=STATE_URL_TEMPLATE.format(self.state_store, "transaction"),
            data={
                "operations": queries,
                "metadata": {"partitionKey": self.partition_key},
            },
            traceparent=traceparent,
        )
        if not response.ok:
            body = await response.text()
            optimistic_write = any(
                operation.get("options", {}).get("concurrency") == "first-write"
                for operation in operations
            )
            if optimistic_write or response.status == 409 or any(
                marker in body.lower() for marker in ("etag", "first-write", "conflict")
            ):
                raise StateStoreConflictError(body)
            raise RuntimeError(f"State store transaction failed: {body}")
