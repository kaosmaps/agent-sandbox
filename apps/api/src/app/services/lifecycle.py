"""Deployment lifecycle hooks service.

Provides webhook-based notifications for container lifecycle events.
Supports configurable hooks per deployment for integration with external systems.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()


class LifecycleEvent(str, Enum):
    """Container lifecycle event types."""

    ON_START = "on_start"
    ON_HEALTHY = "on_healthy"
    ON_UNHEALTHY = "on_unhealthy"
    ON_STOP = "on_stop"
    ON_ERROR = "on_error"
    ON_ARTIFACT = "on_artifact"


@dataclass
class WebhookConfig:
    """Configuration for a lifecycle webhook."""

    url: str
    events: list[LifecycleEvent] = field(default_factory=lambda: list(LifecycleEvent))
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 10.0
    retry_count: int = 3
    retry_delay_seconds: float = 1.0


@dataclass
class HookInvocation:
    """Record of a hook invocation."""

    deployment_id: str
    event: LifecycleEvent
    webhook_url: str
    timestamp: datetime
    success: bool
    status_code: int | None = None
    error: str | None = None
    response_time_ms: float = 0.0


class LifecycleService:
    """Service for managing deployment lifecycle hooks."""

    def __init__(self):
        # deployment_id -> list of webhook configs
        self._hooks: dict[str, list[WebhookConfig]] = {}
        # deployment_id -> list of invocation records
        self._history: dict[str, list[HookInvocation]] = {}
        self._history_limit = 100  # Keep last N invocations per deployment

    def register_hook(
        self,
        deployment_id: str,
        webhook_url: str,
        events: list[LifecycleEvent] | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        """Register a webhook for lifecycle events.

        Args:
            deployment_id: Deployment to monitor
            webhook_url: URL to POST events to
            events: Events to subscribe to (default: all)
            headers: Additional headers to send
            timeout_seconds: Request timeout
        """
        config = WebhookConfig(
            url=webhook_url,
            events=events or list(LifecycleEvent),
            headers=headers or {},
            timeout_seconds=timeout_seconds,
        )

        if deployment_id not in self._hooks:
            self._hooks[deployment_id] = []

        self._hooks[deployment_id].append(config)

        logger.info(
            "lifecycle_hook_registered",
            deployment_id=deployment_id,
            webhook_url=webhook_url,
            events=[e.value for e in config.events],
        )

    def unregister_hooks(self, deployment_id: str) -> int:
        """Remove all hooks for a deployment.

        Args:
            deployment_id: Deployment to clear hooks for

        Returns:
            Number of hooks removed
        """
        count = len(self._hooks.get(deployment_id, []))
        if deployment_id in self._hooks:
            del self._hooks[deployment_id]
        logger.info("lifecycle_hooks_removed", deployment_id=deployment_id, count=count)
        return count

    async def emit(
        self,
        deployment_id: str,
        event: LifecycleEvent,
        data: dict[str, Any] | None = None,
    ) -> list[HookInvocation]:
        """Emit a lifecycle event to all registered webhooks.

        Args:
            deployment_id: Deployment that triggered the event
            event: Type of lifecycle event
            data: Additional event data

        Returns:
            List of invocation records
        """
        hooks = self._hooks.get(deployment_id, [])
        if not hooks:
            return []

        # Filter to hooks that care about this event
        relevant_hooks = [h for h in hooks if event in h.events]
        if not relevant_hooks:
            return []

        logger.info(
            "emitting_lifecycle_event",
            deployment_id=deployment_id,
            event=event.value,
            hook_count=len(relevant_hooks),
        )

        # Build payload
        payload = {
            "deployment_id": deployment_id,
            "event": event.value,
            "timestamp": datetime.utcnow().isoformat(),
            "data": data or {},
        }

        # Invoke all hooks concurrently
        tasks = [
            self._invoke_hook(deployment_id, event, hook, payload)
            for hook in relevant_hooks
        ]
        invocations = await asyncio.gather(*tasks)

        # Store in history
        if deployment_id not in self._history:
            self._history[deployment_id] = []
        self._history[deployment_id].extend(invocations)

        # Trim history
        if len(self._history[deployment_id]) > self._history_limit:
            self._history[deployment_id] = self._history[deployment_id][-self._history_limit:]

        return invocations

    async def _invoke_hook(
        self,
        deployment_id: str,
        event: LifecycleEvent,
        hook: WebhookConfig,
        payload: dict[str, Any],
    ) -> HookInvocation:
        """Invoke a single webhook with retry logic.

        Returns:
            Invocation record
        """
        start_time = datetime.utcnow()
        last_error = None
        status_code = None

        for attempt in range(hook.retry_count):
            try:
                async with httpx.AsyncClient(timeout=hook.timeout_seconds) as client:
                    response = await client.post(
                        hook.url,
                        json=payload,
                        headers={
                            "Content-Type": "application/json",
                            "X-Sandbox-Event": event.value,
                            "X-Sandbox-Deployment": deployment_id,
                            **hook.headers,
                        },
                    )
                    status_code = response.status_code

                    if response.is_success:
                        elapsed = (datetime.utcnow() - start_time).total_seconds() * 1000

                        logger.info(
                            "lifecycle_hook_success",
                            deployment_id=deployment_id,
                            event=event.value,
                            webhook_url=hook.url,
                            status_code=status_code,
                            elapsed_ms=elapsed,
                        )

                        return HookInvocation(
                            deployment_id=deployment_id,
                            event=event,
                            webhook_url=hook.url,
                            timestamp=start_time,
                            success=True,
                            status_code=status_code,
                            response_time_ms=elapsed,
                        )
                    else:
                        last_error = f"HTTP {status_code}"

            except httpx.TimeoutException:
                last_error = "Timeout"
            except httpx.RequestError as e:
                last_error = str(e)
            except Exception as e:
                last_error = str(e)

            # Wait before retry (except on last attempt)
            if attempt < hook.retry_count - 1:
                await asyncio.sleep(hook.retry_delay_seconds)

        # All retries failed
        elapsed = (datetime.utcnow() - start_time).total_seconds() * 1000

        logger.warning(
            "lifecycle_hook_failed",
            deployment_id=deployment_id,
            event=event.value,
            webhook_url=hook.url,
            error=last_error,
            attempts=hook.retry_count,
        )

        return HookInvocation(
            deployment_id=deployment_id,
            event=event,
            webhook_url=hook.url,
            timestamp=start_time,
            success=False,
            status_code=status_code,
            error=last_error,
            response_time_ms=elapsed,
        )

    def get_hooks(self, deployment_id: str) -> list[dict[str, Any]]:
        """Get registered hooks for a deployment."""
        hooks = self._hooks.get(deployment_id, [])
        return [
            {
                "url": h.url,
                "events": [e.value for e in h.events],
                "timeout_seconds": h.timeout_seconds,
            }
            for h in hooks
        ]

    def get_history(
        self,
        deployment_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get hook invocation history for a deployment."""
        history = self._history.get(deployment_id, [])
        return [
            {
                "event": h.event.value,
                "webhook_url": h.webhook_url,
                "timestamp": h.timestamp.isoformat(),
                "success": h.success,
                "status_code": h.status_code,
                "error": h.error,
                "response_time_ms": h.response_time_ms,
            }
            for h in history[-limit:]
        ]


# Global instance
lifecycle_service = LifecycleService()


# Convenience functions for emitting events
async def emit_started(deployment_id: str, image: str, url: str) -> None:
    """Emit deployment started event."""
    await lifecycle_service.emit(
        deployment_id,
        LifecycleEvent.ON_START,
        {"image": image, "url": url},
    )


async def emit_healthy(deployment_id: str, url: str) -> None:
    """Emit container healthy event."""
    await lifecycle_service.emit(
        deployment_id,
        LifecycleEvent.ON_HEALTHY,
        {"url": url},
    )


async def emit_unhealthy(deployment_id: str, reason: str) -> None:
    """Emit container unhealthy event."""
    await lifecycle_service.emit(
        deployment_id,
        LifecycleEvent.ON_UNHEALTHY,
        {"reason": reason},
    )


async def emit_stopped(deployment_id: str, reason: str = "manual") -> None:
    """Emit container stopped event."""
    await lifecycle_service.emit(
        deployment_id,
        LifecycleEvent.ON_STOP,
        {"reason": reason},
    )


async def emit_error(deployment_id: str, error: str) -> None:
    """Emit deployment error event."""
    await lifecycle_service.emit(
        deployment_id,
        LifecycleEvent.ON_ERROR,
        {"error": error},
    )


async def emit_artifact(deployment_id: str, artifact_id: str, filename: str) -> None:
    """Emit artifact uploaded event."""
    await lifecycle_service.emit(
        deployment_id,
        LifecycleEvent.ON_ARTIFACT,
        {"artifact_id": artifact_id, "filename": filename},
    )
