"""Core module: attaches an observer WebSocket endpoint to any FastAPI app
and intercepts outgoing WebSocket messages by wrapping the ASGI callable."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.types import Receive, Scope, Send, Message

logger = logging.getLogger("guardian_tap")

_observers: set[WebSocket] = set()


async def _broadcast(text: str) -> None:
    """Broadcast a raw JSON string to all observers."""
    if not _observers:
        return
    dead: set[WebSocket] = set()
    for obs in _observers:
        try:
            await obs.send_text(text)
        except Exception:
            dead.add(obs)
    _observers.difference_update(dead)


def attach_observer(
    app: FastAPI,
    path: str = "/ws-observe",
) -> None:
    """Attach a GuardianAI observer endpoint to a FastAPI app.

    This does two things:
    1. Adds a /ws-observe WebSocket endpoint for observers to connect to.
    2. Wraps the ASGI app to intercept all outgoing WebSocket messages
       and broadcast them to observers.

    Args:
        app: The FastAPI application instance.
        path: The WebSocket path for observers. Defaults to "/ws-observe".

    Usage:
        from guardian_tap import attach_observer
        attach_observer(app)
    """

    # 1. Add the observer endpoint
    @app.websocket(path)
    async def _guardian_observe(websocket: WebSocket) -> None:
        await websocket.accept()
        _observers.add(websocket)
        logger.info("Guardian observer connected (%d total)", len(_observers))
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            _observers.discard(websocket)
            logger.info("Guardian observer disconnected (%d remaining)", len(_observers))

    # 2. Wrap the app's ASGI __call__ to intercept WebSocket sends
    _original_call = app.__call__

    async def _patched_call(scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "websocket" and scope.get("path") != path:
            async def send_wrapper(message: Message) -> None:
                await send(message)
                if (
                    message.get("type") == "websocket.send"
                    and "text" in message
                    and _observers
                ):
                    await _broadcast(message["text"])

            await _original_call(scope, receive, send_wrapper)
        else:
            await _original_call(scope, receive, send)

    app.__call__ = _patched_call  # type: ignore[method-assign]

    logger.info("guardian-tap attached at %s", path)


async def broadcast(data: dict) -> None:
    """Manually broadcast a JSON payload to all connected observers."""
    await _broadcast(json.dumps(data))
