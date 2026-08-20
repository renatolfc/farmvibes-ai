# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from vibe_agent.data_ops import WORKFLOW_TASK_KEY_PATTERN, DataOpsManager
from vibe_common.constants import RUNS_KEY
from vibe_common.statestore import StateStoreConflictError, TransactionOperation
from vibe_core.datamodel import RunConfig, RunDetails, RunStatus


class FakeStateStore:
    def __init__(self, data: Dict[str, Any]):
        self.data = deepcopy(data)
        self.versions = {key: 1 for key in data}
        self.conflicts_remaining = 0

    async def retrieve(self, key: str) -> Any:
        if key not in self.data:
            raise KeyError(key)
        return deepcopy(self.data[key])

    async def retrieve_with_etag(self, key: str):
        return await self.retrieve(key), str(self.versions[key])

    async def retrieve_bulk(self, keys: List[str]) -> List[Any]:
        return [await self.retrieve(key) for key in keys]

    async def store(self, key: str, value: Any) -> None:
        self.data[key] = asdict(value) if is_dataclass(value) else deepcopy(value)
        self.versions[key] = self.versions.get(key, 0) + 1

    async def transaction(self, operations: List[TransactionOperation]) -> None:
        for operation in operations:
            if "etag" not in operation:
                continue
            if self.conflicts_remaining:
                self.conflicts_remaining -= 1
                self.versions[operation["key"]] += 1
                raise StateStoreConflictError("simulated conflict")
            if operation["etag"] != str(self.versions[operation["key"]]):
                raise StateStoreConflictError("etag mismatch")

        updated = deepcopy(self.data)
        for operation in operations:
            key = operation["key"]
            if operation["operation"] == "delete":
                updated.pop(key, None)
            else:
                value = operation.get("value")
                updated[key] = asdict(value) if is_dataclass(value) else deepcopy(value)
        self.data = updated
        for operation in operations:
            key = operation["key"]
            self.versions[key] = self.versions.get(key, 0) + 1


def run_record(run_id: str, status: RunStatus, tasks: List[str]) -> Dict[str, Any]:
    record = asdict(
        RunConfig(
            name=f"run-{run_id}",
            workflow="helloworld",
            parameters={"keep": True},
            user_input={"input": "preserved"},
            id=run_id,
            details=RunDetails(
                submission_time=datetime.now(),
                end_time=datetime.now() if status != RunStatus.running else None,
                status=status,
            ),
            task_details={},
            spatio_temporal_json=None,
            output="large-encoded-output",
        )
    )
    record["tasks"] = tasks
    return record


def history_manager(
    state: FakeStateStore,
    max_full: int,
    max_compact: int,
    scan_redis_workflow_state: bool = False,
) -> DataOpsManager:
    metadata_store = Mock()
    metadata_store.get_run_ops = AsyncMock(return_value=set())
    metadata_store.find_keys = AsyncMock(return_value=set())
    storage = Mock()
    manager = DataOpsManager(
        storage,
        metadata_store,
        max_full_history_runs=max_full,
        max_compact_history_runs=max_compact,
        scan_redis_workflow_state=scan_redis_workflow_state,
    )
    manager.statestore = state  # type: ignore
    manager._init_locks()
    return manager


@pytest.mark.anyio
async def test_history_tiers_compact_delete_and_preserve_active_runs():
    run_ids = [str(uuid4()) for _ in range(6)]
    statuses = [
        RunStatus.done,
        RunStatus.running,
        RunStatus.done,
        RunStatus.deleted,
        RunStatus.done,
        RunStatus.running,
    ]
    state_data: Dict[str, Any] = {RUNS_KEY: run_ids}
    for run_id, status in zip(run_ids, statuses):
        state_data[run_id] = run_record(run_id, status, ["task"])
        state_data[f"{run_id}-task"] = asdict(RunDetails(status=status))

    state = FakeStateStore(state_data)
    manager = history_manager(state, max_full=2, max_compact=2)

    await manager.maintain_history()

    deleted, active, compacted, compacted_deleted, full, newest_active = run_ids
    assert deleted not in state.data
    assert f"{deleted}-task" not in state.data
    assert state.data[RUNS_KEY] == run_ids[1:]
    manager.metadata_store.get_run_ops.assert_awaited_once_with(deleted)  # type: ignore

    assert state.data[active] == state_data[active]
    assert state.data[f"{active}-task"] == state_data[f"{active}-task"]

    for run_id in (compacted, compacted_deleted):
        summary = state.data[run_id]
        assert summary["history_compacted"] is True
        assert summary["task_details"] == {}
        assert summary["output"] == ""
        assert summary["workflow"] == "helloworld"
        assert summary["parameters"] == {"keep": True}
        assert summary["user_input"] == {"input": "preserved"}
        assert f"{run_id}-task" not in state.data

    for run_id in (full, newest_active):
        assert state.data[run_id] == state_data[run_id]
        assert state.data[f"{run_id}-task"] == state_data[f"{run_id}-task"]


@pytest.mark.parametrize(
    "status",
    [RunStatus.pending, RunStatus.queued, RunStatus.running, RunStatus.deleting],
)
@pytest.mark.parametrize("max_compact", [0, 1])
@pytest.mark.anyio
async def test_nonterminal_runs_are_never_touched(status: RunStatus, max_compact: int):
    run_id = str(uuid4())
    original = run_record(run_id, status, ["task"])
    state = FakeStateStore(
        {
            RUNS_KEY: [run_id],
            run_id: original,
            f"{run_id}-task": asdict(RunDetails(status=status)),
        }
    )
    manager = history_manager(
        state,
        max_full=0,
        max_compact=max_compact,
        scan_redis_workflow_state=True,
    )

    await manager.maintain_history()

    assert state.data[RUNS_KEY] == [run_id]
    assert state.data[run_id] == original
    assert f"{run_id}-task" in state.data
    manager.metadata_store.get_run_ops.assert_not_awaited()  # type: ignore
    manager.metadata_store.find_keys.assert_not_awaited()  # type: ignore


@pytest.mark.anyio
async def test_repeated_concurrent_maintenance_is_idempotent_after_index_conflict():
    run_id = str(uuid4())
    state = FakeStateStore(
        {
            RUNS_KEY: [run_id],
            run_id: run_record(run_id, RunStatus.done, ["task"]),
            f"{run_id}-task": asdict(RunDetails(status=RunStatus.done)),
        }
    )
    state.conflicts_remaining = 1
    manager = history_manager(state, max_full=0, max_compact=0)

    await asyncio.gather(manager.maintain_history(), manager.maintain_history())
    await manager.maintain_history()

    assert state.data[RUNS_KEY] == []
    assert run_id not in state.data
    assert f"{run_id}-task" not in state.data
    manager.metadata_store.get_run_ops.assert_awaited_once_with(run_id)  # type: ignore


@pytest.mark.anyio
async def test_interrupted_retention_cleanup_resumes_before_hard_delete():
    run_id = str(uuid4())
    state = FakeStateStore(
        {
            RUNS_KEY: [run_id],
            run_id: run_record(run_id, RunStatus.done, ["task"]),
            f"{run_id}-task": asdict(RunDetails(status=RunStatus.done)),
        }
    )
    manager = history_manager(state, max_full=0, max_compact=0)
    manager.metadata_store.get_run_ops.side_effect = [  # type: ignore
        RuntimeError("temporary metadata failure"),
        set(),
    ]

    await manager.maintain_history()

    interrupted = RunConfig(**state.data[run_id])
    assert interrupted.details.status == RunStatus.deleting
    assert interrupted.details.reason == manager.retention_deletion_reason
    assert state.data[run_id]["tasks"] == ["task"]
    assert run_id in state.data[RUNS_KEY]
    assert f"{run_id}-task" in state.data

    await manager.maintain_history()

    assert state.data[RUNS_KEY] == []
    assert run_id not in state.data
    assert f"{run_id}-task" not in state.data
    assert manager.metadata_store.get_run_ops.await_count == 2  # type: ignore


@pytest.mark.anyio
async def test_legacy_task_keys_are_discovered_for_local_redis_compaction():
    run_id = str(uuid4())
    record = run_record(run_id, RunStatus.done, ["task"])
    del record["tasks"]
    state = FakeStateStore(
        {
            RUNS_KEY: [run_id],
            run_id: record,
            f"{run_id}-task": asdict(RunDetails(status=RunStatus.done)),
        }
    )
    manager = history_manager(
        state,
        max_full=0,
        max_compact=1,
        scan_redis_workflow_state=True,
    )
    manager.metadata_store.find_keys.return_value = {f"{run_id}-task"}  # type: ignore

    await manager.maintain_history()

    assert state.data[run_id]["history_compacted"] is True
    assert f"{run_id}-task" not in state.data
    manager.metadata_store.find_keys.assert_awaited_once_with(  # type: ignore
        WORKFLOW_TASK_KEY_PATTERN
    )
