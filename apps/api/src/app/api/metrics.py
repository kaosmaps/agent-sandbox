"""Prometheus metrics endpoint.

Exposes deployment and container metrics in Prometheus format.
"""

import time
from typing import Any

import structlog
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from app.services.docker import DockerService

logger = structlog.get_logger()
router = APIRouter()

# Create a custom registry to avoid default collectors
registry = CollectorRegistry()

# Counters
deployments_total = Counter(
    "sandbox_deployments_total",
    "Total number of deployments",
    ["status"],
    registry=registry,
)

artifacts_total = Counter(
    "sandbox_artifacts_total",
    "Total number of artifacts uploaded",
    registry=registry,
)

artifact_commits_total = Counter(
    "sandbox_artifact_commits_total",
    "Total number of artifact commits to git",
    ["status"],
    registry=registry,
)

# Gauges
deployments_active = Gauge(
    "sandbox_deployments_active",
    "Number of currently active deployments",
    registry=registry,
)

containers_running = Gauge(
    "sandbox_containers_running",
    "Number of running containers",
    registry=registry,
)

artifacts_storage_bytes = Gauge(
    "sandbox_artifacts_storage_bytes",
    "Total bytes of stored artifacts",
    registry=registry,
)

# Histograms
deployment_duration_seconds = Histogram(
    "sandbox_deployment_duration_seconds",
    "Time to deploy a container",
    buckets=[0.5, 1, 2, 5, 10, 30, 60, 120],
    registry=registry,
)

artifact_upload_bytes = Histogram(
    "sandbox_artifact_upload_bytes",
    "Size of uploaded artifacts",
    buckets=[1024, 10240, 102400, 1048576, 10485760, 104857600],
    registry=registry,
)


def increment_deployment(status: str = "success") -> None:
    """Increment deployment counter."""
    deployments_total.labels(status=status).inc()


def increment_artifact() -> None:
    """Increment artifact counter."""
    artifacts_total.inc()


def increment_artifact_commit(status: str = "success") -> None:
    """Increment artifact commit counter."""
    artifact_commits_total.labels(status=status).inc()


def observe_deployment_duration(duration: float) -> None:
    """Record deployment duration."""
    deployment_duration_seconds.observe(duration)


def observe_artifact_size(size: int) -> None:
    """Record artifact upload size."""
    artifact_upload_bytes.observe(size)


async def _update_gauges() -> None:
    """Update gauge values from current state."""
    try:
        docker_service = DockerService()
        containers = await docker_service.list_sandbox_containers()

        running_count = sum(1 for c in containers if c.get("status") == "running")
        containers_running.set(running_count)
        deployments_active.set(len(containers))

    except Exception as e:
        logger.warning("gauge_update_failed", error=str(e))


@router.get("/metrics", response_class=PlainTextResponse)
async def get_metrics():
    """Get Prometheus metrics.

    Returns metrics in Prometheus text format for scraping.
    """
    # Update gauges before returning
    await _update_gauges()

    # Generate Prometheus format
    output = generate_latest(registry)

    return PlainTextResponse(
        content=output,
        media_type=CONTENT_TYPE_LATEST,
    )


@router.get("/metrics/json")
async def get_metrics_json() -> dict[str, Any]:
    """Get metrics as JSON for debugging.

    Returns current metric values in JSON format.
    """
    await _update_gauges()

    docker_service = DockerService()
    containers = await docker_service.list_sandbox_containers()

    return {
        "timestamp": time.time(),
        "containers": {
            "total": len(containers),
            "running": sum(1 for c in containers if c.get("status") == "running"),
            "list": containers,
        },
        "counters": {
            "deployments_total": {
                "success": deployments_total.labels(status="success")._value.get(),
                "failed": deployments_total.labels(status="failed")._value.get(),
            },
            "artifacts_total": artifacts_total._value.get(),
            "artifact_commits_total": {
                "success": artifact_commits_total.labels(status="success")._value.get(),
                "failed": artifact_commits_total.labels(status="failed")._value.get(),
            },
        },
    }
