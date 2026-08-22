# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import logging
from copy import deepcopy
from typing import Any, Dict, List, Optional, Set, cast
from uuid import UUID

from aiorwlock import RWLock
from cloudevents.sdk.event import v1
from dapr.conf import settings
from fastapi import Request
from hydra_zen import builds
from opentelemetry import trace

from vibe_agent.cache_metadata_store import (
    CacheMetadataStoreProtocol,
    CacheMetadataStoreProtocolConfig,
)
from vibe_agent.storage.storage import Storage, StorageConfig
from vibe_common.constants import (
    CONTROL_STATUS_PUBSUB,
    RUNS_KEY,
    STATUS_PUBSUB_TOPIC,
    TRACEPARENT_HEADER_KEY,
    WORKFLOW_REQUEST_PUBSUB_TOPIC,
)
from vibe_common.dapr import dapr_ready
from vibe_common.dropdapr import App, TopicEventResponse, TopicEventResponseStatus
from vibe_common.messaging import (
    ExecuteReplyContent,
    MessageType,
    WorkMessage,
    accept_or_fail_event_async,
    extract_message_header_from_event,
    run_id_from_traceparent,
)
from vibe_common.schemas import OpRunId, OpRunIdDict
from vibe_common.statestore import StateStore, StateStoreConflictError, TransactionOperation
from vibe_common.telemetry import add_trace, setup_telemetry, update_telemetry_context
from vibe_core.data.core_types import OpIOType
from vibe_core.datamodel import RunConfig, RunStatus
from vibe_core.logconfig import LOG_BACKUP_COUNT, MAX_LOG_FILE_BYTES, configure_logging
from vibe_core.utils import ensure_list

DEFAULT_MAX_FULL_HISTORY_RUNS = 100
DEFAULT_MAX_COMPACT_HISTORY_RUNS = 900
HISTORY_MAINTENANCE_BATCH_SIZE = 10
HISTORY_MAINTENANCE_SCAN_SIZE = 100
HISTORY_MAINTENANCE_INTERVAL_S = 60
HISTORY_INDEX_UPDATE_RETRIES = 3
WORKFLOW_TASK_KEY_PATTERN = "????????-????-????-????-????????????-*"


class DataOpsManager:
    """
    The DataOpsManager is responsible for managing metadata about the system's cached data and
    coordinating data operations.

    Assumptions this code makes:
    - Once a workflow run is complete, its metadata (i.e. `RunConfig` in StateStore) and cached
      data in Storage is immutable outside of the DataOpsManager.
    - Once a op run is complete its cached data (i.e. metadata/catalog) and assets in Storage
      are immutable.

    Notes about locks:
    - The way metadata_store_lock essentially serializes all requests to the metadata store
      whether they be add ref or delete ref requests. To make this more efficient in the future,
      we can create a lock that allows many add ref requests to go through at a time but only one
      delete ref request to execute at a time.
    """

    app: App
    metadata_store_lock: RWLock
    otel_service_name: str
    statestore_lock: asyncio.Lock
    history_maintenance_lock: asyncio.Lock
    history_maintenance_wakeup: asyncio.Event
    history_maintenance_task: Optional[asyncio.Task[None]]
    history_compaction_offset: int
    history_deletion_offset: int
    legacy_task_keys: Optional[Dict[str, List[str]]]

    user_deletion_reason = "Deletion requested by user"
    retention_deletion_reason = "Deletion requested by workflow history retention"

    def __init__(
        self,
        storage: Storage,
        metadata_store: CacheMetadataStoreProtocol,
        pubsubname: str = CONTROL_STATUS_PUBSUB,
        status_topic: str = STATUS_PUBSUB_TOPIC,
        delete_workflow_topic: str = WORKFLOW_REQUEST_PUBSUB_TOPIC,
        port: int = settings.HTTP_APP_PORT,
        logdir: Optional[str] = None,
        max_log_file_bytes: int = MAX_LOG_FILE_BYTES,
        log_backup_count: int = LOG_BACKUP_COUNT,
        loglevel: Optional[str] = None,
        otel_service_name: str = "",
        max_full_history_runs: Optional[int] = None,
        max_compact_history_runs: Optional[int] = None,
        scan_redis_workflow_state: bool = False,
    ):
        if (max_full_history_runs is None) != (max_compact_history_runs is None):
            raise ValueError("Both workflow history limits must be configured together")
        if (
            max_full_history_runs is not None
            and max_compact_history_runs is not None
            and (max_full_history_runs < 0 or max_compact_history_runs < 0)
        ):
            raise ValueError("Workflow history limits must be non-negative")

        self.app = App()
        self.port = port
        self.pubsubname = pubsubname
        self.status_topic = status_topic
        self.delete_workflow_topic = delete_workflow_topic
        self.storage = storage
        self.metadata_store = metadata_store
        self.statestore = StateStore()
        self.logdir = logdir
        self.max_log_file_bytes = max_log_file_bytes
        self.log_backup_count = log_backup_count
        self.loglevel = loglevel
        self.otel_service_name = otel_service_name
        self.max_full_history_runs = max_full_history_runs
        self.max_compact_history_runs = max_compact_history_runs
        self.scan_redis_workflow_state = scan_redis_workflow_state
        self.legacy_task_keys = None
        self.history_maintenance_task = None

        self._setup_routes()

    def _init_locks(self):
        logging.debug("Creating locks")
        self.metadata_store_lock = RWLock(fast=True)
        self.statestore_lock = asyncio.Lock()
        self.history_maintenance_lock = asyncio.Lock()
        self.history_maintenance_wakeup = asyncio.Event()
        self.history_compaction_offset = 0
        self.history_deletion_offset = 0

    def _setup_routes(self):
        @self.app.startup()
        def startup():
            if (
                self.history_maintenance_task is not None
                and not self.history_maintenance_task.done()
            ):
                return
            # locks have to be be created on the app's (uvicorn's) event loop
            self._init_locks()
            if self.max_full_history_runs is None:
                return
            # Dapr is not available until the app startup hook returns.
            self.history_maintenance_task = asyncio.create_task(
                self._maintain_history_periodically()
            )

        @self.app.shutdown()
        async def shutdown():
            if self.history_maintenance_task is None:
                return
            self.history_maintenance_task.cancel()
            try:
                await self.history_maintenance_task
            except asyncio.CancelledError:
                pass
            self.history_maintenance_task = None

        @self.app.subscribe_async(self.pubsubname, self.status_topic)
        async def fetch_work(event: v1.Event) -> TopicEventResponse:
            return await self.fetch_work(self.status_topic, event)

        @self.app.subscribe_async(self.pubsubname, self.delete_workflow_topic)
        async def manage_workflow(event: v1.Event):
            await self.handle_workflow_event(self.delete_workflow_topic, event)

        @self.app.method(name="add_refs/{run_id}")
        async def add_refs(
            request: Request, run_id: str, op_run_id_dict: OpRunIdDict, output: OpIOType
        ) -> TopicEventResponse:
            try:
                traceparent = request.headers.get(TRACEPARENT_HEADER_KEY)
                if traceparent:
                    update_telemetry_context(traceparent)
                else:
                    logging.warning("No traceparent found in request headers.")

                with trace.get_tracer(__name__).start_as_current_span("add_refs"):
                    await self.add_references(run_id, OpRunId(**op_run_id_dict), output)
                    return TopicEventResponseStatus.success
            except Exception as e:
                logging.error(
                    f"Error adding references from service invocation for run id {run_id}: {e}"
                )
                return TopicEventResponseStatus.drop

    async def fetch_work(self, channel: str, event: v1.Event) -> TopicEventResponse:
        @add_trace
        async def success_callback(message: WorkMessage) -> TopicEventResponse:
            if not message.is_valid_for_channel(channel):
                logging.warning(
                    f"Received invalid message {message} for channel {channel}. Dropping it."
                )
                return TopicEventResponseStatus.drop

            if message.header.type == MessageType.execute_reply:
                content = cast(ExecuteReplyContent, message.content)
                logging.debug(
                    f"Received execute reply for run id {message.run_id} "
                    f"(op name {content.cache_info.name}, op hash {content.cache_info.hash})."
                )

                run_id = str(message.run_id)
                op_run_id = OpRunId(content.cache_info.name, content.cache_info.hash)
                await self.add_references(run_id, op_run_id, content.output)

            return TopicEventResponseStatus.success

        @add_trace
        async def failure_callback(
            event: v1.Event, e: Exception, traceback: List[str]
        ) -> TopicEventResponse:
            run_id = str(run_id_from_traceparent(event.id))
            logging.error(f"Failed to add references for run id {run_id}: {e}")
            return TopicEventResponseStatus.drop

        update_telemetry_context(extract_message_header_from_event(event).current_trace_parent)
        with trace.get_tracer(__name__).start_as_current_span("fetch_work"):
            return await accept_or_fail_event_async(event, success_callback, failure_callback)

    async def handle_workflow_event(self, channel: str, event: v1.Event):
        async def success_callback(message: WorkMessage) -> TopicEventResponse:
            if not message.is_valid_for_channel(channel):
                logging.warning(
                    f"Received invalid message {message} for channel {channel}. Dropping it."
                )
                return TopicEventResponseStatus.drop

            if message.header.type == MessageType.workflow_deletion_request:
                logging.debug(f"Received deletion request for run id {message.run_id}.")

                run_id = str(message.run_id)
                await self.delete_workflow_run(run_id)

            if message.header.type in {
                MessageType.workflow_execution_request,
                MessageType.workflow_deletion_request,
            }:
                task = self.history_maintenance_task
                if task is not None and not task.done():
                    self.history_maintenance_wakeup.set()

            return TopicEventResponseStatus.success

        async def failure_callback(
            event: v1.Event, e: Exception, traceback: List[str]
        ) -> TopicEventResponse:
            run_id = str(run_id_from_traceparent(event.id))
            logging.error(f"Failed to delete run id {run_id}: {e}")
            return TopicEventResponseStatus.drop

        return await accept_or_fail_event_async(event, success_callback, failure_callback)

    def get_asset_ids(self, output: OpIOType) -> Set[str]:
        """
        Given op output as a OpIOTypes, returns the set of asset ids that are referenced in the
        output.

        :param output: The op output as OpIOType

        :return: The set of asset ids referenced in the output
        """
        # TODO: this should probably be moved into vibe_core.utils
        asset_ids: Set[str] = set()
        for output_item in output.values():
            output_item_list = ensure_list(output_item)
            for i in output_item_list:
                asset_ids.update(i["assets"].keys())
        return asset_ids

    async def add_references(self, run_id: str, op_run_id: OpRunId, output: OpIOType) -> None:
        # many requests to add references can be processed simultaneously assuming Redis SADD used
        async with self.metadata_store_lock.reader_lock:
            try:
                asset_ids = self.get_asset_ids(output)
                await self.metadata_store.store_references(run_id, op_run_id, asset_ids)
                logging.info(
                    f"Successfully added references for run id {run_id} "
                    f"(op name {op_run_id.name}, op hash {op_run_id.hash})."
                )
            except Exception:
                logging.exception(
                    f"Failed to add references for run id {run_id} "
                    f"(op name {op_run_id.name}, op hash {op_run_id.hash})."
                )
                raise

    def _can_delete(self, run_config: RunConfig) -> bool:
        can_delete = RunStatus.finished(run_config.details.status)

        if not can_delete:
            if run_config.details.status == RunStatus.deleting:
                logging.warning(
                    f"Run {run_config.id} is already being deleted. Will not process request."
                )
            elif run_config.details.status == RunStatus.deleted:
                logging.warning(
                    f"Run {run_config.id} has already been deleted. Will not process request."
                )
            else:
                logging.warning(
                    f"Cannot delete run {run_config.id} with status {run_config.details.status}."
                )

        return can_delete

    async def _init_delete(self, run_id: str, deletion_reason: str = user_deletion_reason) -> bool:
        async with self.statestore_lock:  # type: ignore
            # Using an async lock to ensure two deletion requests for the same workflow run don't
            # get processed at the same time.
            # The data ops manager will only delete a workflow if it is in a finished status.
            # The assumption is once the workflow is finished, the RunConfig will not change in the
            # statestore (i.e. the status will not change) outside of the Data Ops Manager so it is
            # sufficient to use asyncio lock in the Data Ops manager.
            run_data = deepcopy(await self.statestore.retrieve(str(run_id)))
            run_config = RunConfig(**run_data)

            if not self._can_delete(run_config):
                return False

            run_data["details"]["status"] = RunStatus.deleting
            run_data["details"]["reason"] = deletion_reason
            await self.statestore.store(run_id, run_data)
            return True

    async def _finalize_delete(self, run_id: str) -> None:
        async with self.statestore_lock:  # type: ignore
            run_data = deepcopy(await self.statestore.retrieve(str(run_id)))
            run_data["details"]["status"] = RunStatus.deleted
            run_data["output"] = ""
            await self.statestore.store(run_id, run_data)

    async def _maintain_history_safely(self) -> bool:
        try:
            await self.maintain_history()
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Failed to maintain workflow run history")
            return False

    async def _maintain_history_periodically(self) -> None:
        while True:
            self.history_maintenance_wakeup.clear()
            if not await self._maintain_history_safely():
                await asyncio.sleep(HISTORY_MAINTENANCE_INTERVAL_S)
                continue
            try:
                await asyncio.wait_for(
                    self.history_maintenance_wakeup.wait(),
                    HISTORY_MAINTENANCE_INTERVAL_S,
                )
            except asyncio.TimeoutError:
                pass

    async def maintain_history(self) -> None:
        """Compact and delete a bounded slice of old workflow history."""
        if self.max_full_history_runs is None or self.max_compact_history_runs is None:
            return

        async with self.history_maintenance_lock:  # type: ignore
            try:
                run_ids: List[str] = await self.statestore.retrieve(RUNS_KEY)
            except KeyError:
                return

            full_history_start = max(0, len(run_ids) - self.max_full_history_runs)
            compact_history_start = max(0, full_history_start - self.max_compact_history_runs)
            deletion_candidates = run_ids[:compact_history_start]
            compaction_candidates = run_ids[compact_history_start:full_history_start]

            deletion_scan: List[str] = []
            if deletion_candidates:
                start = self.history_deletion_offset % len(deletion_candidates)
                ordered_candidates = deletion_candidates[start:] + deletion_candidates[:start]
                deletion_scan = ordered_candidates[:HISTORY_MAINTENANCE_SCAN_SIZE]
                self.history_deletion_offset = (start + len(deletion_scan)) % len(
                    deletion_candidates
                )

            deleted = 0
            for run_id in deletion_scan:
                if deleted >= HISTORY_MAINTENANCE_BATCH_SIZE:
                    break
                try:
                    if await self._delete_history_run(run_id):
                        deleted += 1
                except Exception:
                    logging.exception(f"Failed to delete retained workflow run {run_id}")

            compaction_scan: List[str] = []
            if compaction_candidates:
                offset = self.history_compaction_offset % len(compaction_candidates)
                end = len(compaction_candidates) - offset
                start = max(0, end - HISTORY_MAINTENANCE_SCAN_SIZE)
                compaction_scan = compaction_candidates[start:end]
                self.history_compaction_offset = (offset + len(compaction_scan)) % len(
                    compaction_candidates
                )

            compacted = 0
            for run_id in reversed(compaction_scan):
                if compacted >= HISTORY_MAINTENANCE_BATCH_SIZE:
                    break
                try:
                    if await self._compact_history_run(run_id):
                        compacted += 1
                except Exception:
                    logging.exception(f"Failed to compact workflow run {run_id}")

    async def _compact_history_run(self, run_id: str) -> bool:
        async with self.statestore_lock:  # type: ignore
            try:
                run_data = await self.statestore.retrieve(run_id)
            except KeyError:
                return False

            run_config = RunConfig(**run_data)
            status = run_config.details.status
            if not (RunStatus.finished(status) or status == RunStatus.deleted):
                return False
            if run_config.history_compacted:
                return False

            task_keys = await self._get_task_state_keys(run_id, run_data)
            run_config.task_details = {}
            run_config.output = ""
            run_config.history_compacted = True
            operations = [TransactionOperation(key=key, operation="delete") for key in task_keys]
            operations.append(
                TransactionOperation(key=run_id, operation="upsert", value=run_config)
            )
            await self.statestore.transaction(operations)
            if self.legacy_task_keys is not None:
                self.legacy_task_keys.pop(run_id, None)
            logging.info(f"Compacted workflow run history for {run_id}")
            return True

    async def _delete_history_run(self, run_id: str) -> bool:
        try:
            run_data = await self.statestore.retrieve(run_id)
        except KeyError:
            return await self._remove_history_entry(run_id, [])

        run_config = RunConfig(**run_data)
        status = run_config.details.status
        retention_deleting = (
            status == RunStatus.deleting
            and run_config.details.reason == self.retention_deletion_reason
        )
        if not (status == RunStatus.deleted or retention_deleting or RunStatus.finished(status)):
            return False

        task_keys = await self._get_task_state_keys(run_id, run_data)
        if status == RunStatus.deleted:
            cleanup_complete = True
        elif retention_deleting:
            cleanup_complete = await self._finish_delete_workflow_run(run_id)
        else:
            cleanup_complete = await self.delete_workflow_run(
                run_id, self.retention_deletion_reason
            )

        if not cleanup_complete:
            return False

        state_keys = [run_id] + task_keys
        await self._remove_history_entry(run_id, state_keys)
        if self.legacy_task_keys is not None:
            self.legacy_task_keys.pop(run_id, None)
        logging.info(f"Deleted workflow run history for {run_id}")
        return True

    async def _remove_history_entry(self, run_id: str, state_keys: List[str]) -> bool:
        for attempt in range(HISTORY_INDEX_UPDATE_RETRIES):
            try:
                run_ids, etag = await self.statestore.retrieve_with_etag(RUNS_KEY)
            except KeyError:
                if state_keys:
                    await self.statestore.transaction(
                        [TransactionOperation(key=key, operation="delete") for key in state_keys]
                    )
                return bool(state_keys)

            if etag is None:
                raise RuntimeError("State store did not return an ETag for the workflow run index")

            indexed = run_id in run_ids
            updated_run_ids = [candidate for candidate in run_ids if candidate != run_id]
            operations = [TransactionOperation(key=key, operation="delete") for key in state_keys]
            operations.append(
                TransactionOperation(
                    key=RUNS_KEY,
                    operation="upsert",
                    value=updated_run_ids,
                    etag=etag,
                    options={"concurrency": "first-write", "consistency": "strong"},
                )
            )
            try:
                await self.statestore.transaction(operations)
                return indexed or bool(state_keys)
            except StateStoreConflictError:
                if attempt + 1 == HISTORY_INDEX_UPDATE_RETRIES:
                    raise
        return False

    async def _get_task_state_keys(self, run_id: str, run_data: Dict[str, Any]) -> List[str]:
        task_keys = [f"{run_id}-{task}" for task in run_data.get("tasks", [])]
        if task_keys or not self.scan_redis_workflow_state:
            return task_keys

        if self.legacy_task_keys is None:
            legacy_task_keys: Dict[str, List[str]] = {}
            for key in await self.metadata_store.find_keys(WORKFLOW_TASK_KEY_PATTERN):
                candidate_run_id = key[:36]
                try:
                    UUID(candidate_run_id)
                except ValueError:
                    continue
                legacy_task_keys.setdefault(candidate_run_id, []).append(key)
            self.legacy_task_keys = legacy_task_keys

        task_keys = sorted(self.legacy_task_keys.get(run_id, []))
        if task_keys:
            logging.info(
                f"Found {len(task_keys)} legacy task state key(s) for workflow run {run_id}"
            )
        return task_keys

    async def delete_op_run(self, op_run: OpRunId) -> None:
        # TODO: the following two calls may be able to be combined into one call to a Lua script
        # (need to learn more about Lua scripts)
        op_asset_ids = await self.metadata_store.get_op_assets(op_run)
        assets_to_ops = await self.metadata_store.get_assets_refs(op_asset_ids)

        for asset_id in op_asset_ids:
            asset_ops = assets_to_ops[asset_id]

            if op_run not in asset_ops:
                logging.warning(
                    f"Inconsistent state in metadata store: asset {asset_id} does not contain "
                    f"reference to {op_run}."
                )
                continue

            if len(asset_ops) == 1:
                # TODO: aiofiles or ??
                logging.debug(f"Removing asset {asset_id} from storage.")
                self.storage.asset_manager.remove(asset_id)

        # TODO: aiofiles or ??
        logging.debug(f"Removing op run catalog {op_run} from storage.")
        self.storage.remove(op_run)
        await self.metadata_store.remove_op_asset_refs(op_run, op_asset_ids)

    async def _finish_delete_workflow_run(self, run_id: str) -> bool:
        """Finish idempotent cleanup after a run has entered deleting state."""
        op_runs = await self.metadata_store.get_run_ops(run_id)

        for op_run in op_runs:
            # (re)grabbing write lock for each op so as not to starve other requests due to delete
            async with self.metadata_store_lock.writer_lock:
                op_wf_run_ids = await self.metadata_store.get_op_workflow_runs(op_run)

                if run_id not in op_wf_run_ids:
                    logging.warning(
                        f"Inconsistent state in metadata store: op {op_run} does not contain "
                        f"reference to workflow run {run_id}."
                    )
                elif len(op_wf_run_ids) == 1:
                    await self.delete_op_run(op_run)

                await self.metadata_store.remove_workflow_op_refs(run_id, op_run)

        await self._finalize_delete(run_id)
        return True

    async def delete_workflow_run(
        self, run_id: str, deletion_reason: str = user_deletion_reason
    ) -> bool:
        if not await self._init_delete(run_id, deletion_reason):
            return False

        return await self._finish_delete_workflow_run(run_id)

    async def run(self):
        appname = "terravibes-data-ops"
        configure_logging(
            default_level=self.loglevel,
            appname=appname,
            logdir=self.logdir,
            max_log_file_bytes=self.max_log_file_bytes,
            log_backup_count=self.log_backup_count,
            logfile=f"{appname}.log",
        )

        if self.otel_service_name:
            setup_telemetry(appname, self.otel_service_name)

        await self.start_service()

    @dapr_ready
    async def start_service(self):
        logging.info(f"Starting data ops manager listening on port {self.port}")
        await self.app.run_async(self.port)


DataOpsConfig = builds(
    DataOpsManager,
    port=settings.GRPC_APP_PORT,
    pubsubname=CONTROL_STATUS_PUBSUB,
    status_topic=STATUS_PUBSUB_TOPIC,
    metadata_store=CacheMetadataStoreProtocolConfig,
    storage=StorageConfig,
    logdir=None,
    max_log_file_bytes=MAX_LOG_FILE_BYTES,
    log_backup_count=LOG_BACKUP_COUNT,
    loglevel=None,
    max_full_history_runs=None,
    max_compact_history_runs=None,
    scan_redis_workflow_state=False,
    otel_service_name="",
)
