"""Core module: attaches an observer WebSocket endpoint to any FastAPI app
and intercepts outgoing WebSocket messages via ASGI middleware."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Awaitable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.types import ASGIApp, Receive, Scope, Send, Message

logger = logging.getLogger("guardian_tap")

# Global set of observer connections
_observers: set[WebSocket] = set()


async def _broadcast_to_observers(data: dict) -> None:
    """Send a JSON payload to all connected observers."""
    if not _observers:
        return
    dead: set[WebSocket] = set()
    for obs in _observers:
        try:
            await obs.send_json(data)
        except Exception:
            dead.add(obs)
    _observers.difference_update(dead)


class _ObserverMiddleware:
    """ASGI middleware that intercepts WebSocket send messages and broadcasts them."""

    def __init__(self, app: ASGIApp, observe_path: str = "/ws-observe") -> None:
        self.app = app
        self.observe_path = observe_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "websocket" or scope["path"] == self.observe_path:
            await self.app(scope, receive, send)
            return

        # Wrap send to intercept outgoing WebSocket text messages
        async def send_wrapper(message: Message) -> None:
            await send(message)
            if message["type"] == "websocket.send" and "text" in message:
                try:
                    data = json.loads(message["text"])
                    if isinstance(data, dict):
                        await _broadcast_to_observers(data)
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

        await self.app(scope, receive, send_wrapper)


def attach_observer(
    app: FastAPI,
    path: str = "/ws-observe",
) -> None:
    """Attach a GuardianAI observer endpoint to a FastAPI app.

    Args:
        app: The FastAPI application instance.
        path: The WebSocket path for observers to connect to.

    Usage:
        from guardian_tap import attach_observer
        attach_observer(app)
    """

    # 1. Add the observer WebSocket endpoint
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

    # 2. Add ASGI middleware to intercept WebSocket messages
    app.add_middleware(_ObserverMiddleware, observe_path=path)

    logger.info("guardian-tap attached at %s", path)


async def broadcast(data: dict) -> None:
    """Manually broadcast a JSON payload to all connected observers.

    Use this to send custom events:
        import guardian_tap
        await guardian_tap.broadcast({"type": "custom_event", "data": {...}})
    """
    await _broadcast_to_observers(data)
