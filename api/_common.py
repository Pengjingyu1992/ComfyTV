import asyncio
import logging
from typing import Any, Coroutine

from server import PromptServer


routes = PromptServer.instance.routes
_log = logging.getLogger(__name__)

_TASKS: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, None], name: str) -> None:
    task = asyncio.create_task(coro, name=name)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def broadcast_entry_event(event: str, project_id: str, payload: dict) -> None:
    try:
        PromptServer.instance.send_sync(
            "comfytv-entries",
            {"event": event, "project_id": project_id, **payload},
        )
    except Exception:
        _log.exception("[ComfyTV/entries] broadcast failed")


def broadcast_asset_event(event: str, payload: dict) -> None:
    try:
        PromptServer.instance.send_sync(
            "comfytv-assets",
            {"event": event, **payload},
        )
    except Exception:
        _log.exception("[ComfyTV/assets] broadcast failed")


def broadcast_workflow_event(event: str, payload: dict) -> None:
    try:
        PromptServer.instance.send_sync(
            "comfytv-workflows",
            {"event": event, **payload},
        )
    except Exception:
        _log.exception("[ComfyTV/workflows] broadcast failed")


def broadcast_stage_param_event(event: str, payload: dict) -> None:
    try:
        PromptServer.instance.send_sync(
            "comfytv-stage-params",
            {"event": event, **payload},
        )
    except Exception:
        _log.exception("[ComfyTV/stage_params] broadcast failed")
