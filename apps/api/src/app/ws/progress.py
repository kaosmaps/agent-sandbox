"""WebSocket endpoint for deployment progress updates.

Provides real-time deployment lifecycle events via WebSocket.
"""

import asyncio
import json
from collections import defaultdict
from datetime import datetime
from enum import Enum
from typing import Any

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = structlog.get_logger()
router = APIRouter()


class EventType(str, Enum):
    """Deployment lifecycle event types."""

    CONNECTED = "connected"
    STARTED = "started"
    PULLING = "pulling"
    HEALTHY = "healthy"
    LOG_LINE = "log_line"
    ARTIFACT_UPLOADED = "artifact_uploaded"
    COMPLETED = "completed"
    FAILED = "failed"
    DISCONNECTED = "disconnected"


class ConnectionManager:
    """Manages WebSocket connections per deployment."""

    def __init__(self):
        # deployment_id -> list of WebSocket connections
        self.connections: dict[str, list[WebSocket]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def connect(self, deployment_id: str, websocket: WebSocket) -> None:
        """Accept a new WebSocket connection."""
        await websocket.accept()
        async with self._lock:
            self.connections[deployment_id].append(websocket)
        logger.info(
            "websocket_connected",
            deployment_id=deployment_id,
            total_connections=len(self.connections[deployment_id]),
        )

    async def disconnect(self, deployment_id: str, websocket: WebSocket) -> None:
        """Remove a WebSocket connection."""
        async with self._lock:
            if websocket in self.connections[deployment_id]:
                self.connections[deployment_id].remove(websocket)
            if not self.connections[deployment_id]:
                del self.connections[deployment_id]
        logger.info("websocket_disconnected", deployment_id=deployment_id)

    async def broadcast(
        self,
        deployment_id: str,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Broadcast an event to all subscribers of a deployment."""
        message = {
            "event": event_type.value,
            "deployment_id": deployment_id,
            "timestamp": datetime.utcnow().isoformat(),
            "data": data or {},
        }
        message_json = json.dumps(message)

        async with self._lock:
            subscribers = self.connections.get(deployment_id, []).copy()

        disconnected = []
        for websocket in subscribers:
            try:
                await websocket.send_text(message_json)
            except Exception as e:
                logger.warning("websocket_send_failed", error=str(e))
                disconnected.append(websocket)

        # Clean up disconnected sockets
        for ws in disconnected:
            await self.disconnect(deployment_id, ws)

    async def broadcast_all(
        self,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Broadcast an event to all connected clients."""
        async with self._lock:
            all_deployments = list(self.connections.keys())

        for deployment_id in all_deployments:
            await self.broadcast(deployment_id, event_type, data)

    def get_subscriber_count(self, deployment_id: str) -> int:
        """Get number of subscribers for a deployment."""
        return len(self.connections.get(deployment_id, []))

    def get_all_subscriber_counts(self) -> dict[str, int]:
        """Get subscriber counts for all deployments."""
        return {dep_id: len(conns) for dep_id, conns in self.connections.items()}


# Global connection manager
manager = ConnectionManager()


@router.websocket("/ws/progress/{deployment_id}")
async def deployment_progress(websocket: WebSocket, deployment_id: str):
    """WebSocket endpoint for deployment progress updates.

    Clients connect to receive real-time events about a deployment's lifecycle:
    - started: Container creation initiated
    - pulling: Image being pulled
    - healthy: Container health check passed
    - log_line: Container log output
    - artifact_uploaded: New artifact available
    - completed: Deployment finished successfully
    - failed: Deployment failed
    """
    await manager.connect(deployment_id, websocket)

    # Send initial connection confirmation
    await websocket.send_json({
        "event": EventType.CONNECTED.value,
        "deployment_id": deployment_id,
        "timestamp": datetime.utcnow().isoformat(),
        "data": {
            "message": f"Connected to progress stream for {deployment_id}",
            "subscribers": manager.get_subscriber_count(deployment_id),
        },
    })

    try:
        # Keep connection alive and handle incoming messages
        while True:
            try:
                # Wait for messages (ping/pong or commands)
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=30.0,  # 30 second timeout for ping
                )

                # Handle ping
                if data == "ping":
                    await websocket.send_text("pong")
                else:
                    # Echo back any other message for debugging
                    await websocket.send_json({
                        "event": "echo",
                        "data": data,
                    })

            except asyncio.TimeoutError:
                # Send keepalive
                try:
                    await websocket.send_json({
                        "event": "keepalive",
                        "timestamp": datetime.utcnow().isoformat(),
                    })
                except Exception:
                    break

    except WebSocketDisconnect:
        logger.info("websocket_client_disconnected", deployment_id=deployment_id)
    except Exception as e:
        logger.error("websocket_error", deployment_id=deployment_id, error=str(e))
    finally:
        await manager.disconnect(deployment_id, websocket)


@router.get("/ws/status")
async def websocket_status() -> dict[str, Any]:
    """Get WebSocket connection status.

    Returns number of active connections per deployment.
    """
    return {
        "connections": manager.get_all_subscriber_counts(),
        "total": sum(manager.get_all_subscriber_counts().values()),
    }


# Helper functions for broadcasting events from other parts of the application


async def emit_started(deployment_id: str, image: str) -> None:
    """Emit deployment started event."""
    await manager.broadcast(
        deployment_id,
        EventType.STARTED,
        {"image": image},
    )


async def emit_pulling(deployment_id: str, image: str) -> None:
    """Emit image pulling event."""
    await manager.broadcast(
        deployment_id,
        EventType.PULLING,
        {"image": image},
    )


async def emit_healthy(deployment_id: str, url: str) -> None:
    """Emit container healthy event."""
    await manager.broadcast(
        deployment_id,
        EventType.HEALTHY,
        {"url": url},
    )


async def emit_log_line(deployment_id: str, line: str) -> None:
    """Emit container log line."""
    await manager.broadcast(
        deployment_id,
        EventType.LOG_LINE,
        {"line": line},
    )


async def emit_artifact_uploaded(
    deployment_id: str,
    artifact_id: str,
    filename: str,
) -> None:
    """Emit artifact uploaded event."""
    await manager.broadcast(
        deployment_id,
        EventType.ARTIFACT_UPLOADED,
        {"artifact_id": artifact_id, "filename": filename},
    )


async def emit_completed(deployment_id: str, url: str, container_id: str) -> None:
    """Emit deployment completed event."""
    await manager.broadcast(
        deployment_id,
        EventType.COMPLETED,
        {"url": url, "container_id": container_id},
    )


async def emit_failed(deployment_id: str, error: str) -> None:
    """Emit deployment failed event."""
    await manager.broadcast(
        deployment_id,
        EventType.FAILED,
        {"error": error},
    )
