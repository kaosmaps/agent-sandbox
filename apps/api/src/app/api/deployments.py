"""Deployment webhook receiver and management API."""

import secrets
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.core.config import settings
from app.services.docker import DockerService

logger = structlog.get_logger()
router = APIRouter()


class DeployRequest(BaseModel):
    """Deployment request from NanoSwarm API."""

    image: str = Field(..., description="Docker image to deploy (e.g., ghcr.io/kaosmaps/agent-sandbox:task-abc123)")
    path_prefix: str = Field(..., description="URL path prefix (e.g., abc123xyz)")
    port: int = Field(default=3000, description="Container port to expose")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables")
    ttl_minutes: int = Field(default=60, description="Time-to-live in minutes (0 = no expiry)")


class DeployResponse(BaseModel):
    """Deployment response."""

    status: str
    deployment_id: str
    url: str
    container_id: str | None = None
    error: str | None = None


class ResourceUsage(BaseModel):
    """Container resource usage."""

    cpu_percent: float = 0.0
    memory_bytes: int = 0
    memory_limit_bytes: int = 0
    memory_percent: float = 0.0


class Deployment(BaseModel):
    """Deployment information."""

    id: str
    path_prefix: str
    image: str
    port: int
    url: str
    container_id: str | None
    status: str
    created_at: datetime
    ttl_minutes: int = 60


class EnhancedDeployment(BaseModel):
    """Enhanced deployment information with runtime details."""

    id: str
    path_prefix: str
    image: str
    port: int
    url: str
    container_id: str | None
    status: str
    created_at: datetime
    ttl_minutes: int = 60
    # Enhanced fields
    container_state: str = "unknown"
    health_status: str = "unknown"
    uptime_seconds: float = 0.0
    last_health_check: datetime | None = None
    resource_usage: ResourceUsage | None = None
    # Related URLs
    logs_url: str = ""
    artifacts_url: str = ""
    metrics_url: str = ""
    websocket_url: str = ""


# In-memory storage (would be database in production)
_deployments: dict[str, Deployment] = {}


def _verify_secret(x_sandbox_secret: str | None) -> None:
    """Verify webhook secret."""
    if settings.WEBHOOK_SECRET and x_sandbox_secret != settings.WEBHOOK_SECRET:
        logger.warning("invalid_webhook_secret")
        raise HTTPException(status_code=401, detail="Invalid webhook secret")


@router.post("/webhook/deploy", response_model=DeployResponse)
async def deploy_container(
    request: DeployRequest,
    x_sandbox_secret: str | None = Header(None, alias="X-Sandbox-Secret"),
):
    """Deploy a container from a Docker image.

    Called by NanoSwarm API when an agent completes a task.
    """
    _verify_secret(x_sandbox_secret)

    deployment_id = request.path_prefix or secrets.token_hex(6)
    container_name = f"{settings.CONTAINER_PREFIX}-{deployment_id}"
    url = f"https://{settings.SANDBOX_DOMAIN}/{deployment_id}/"

    logger.info(
        "deploying_container",
        image=request.image,
        path_prefix=deployment_id,
        port=request.port,
    )

    try:
        docker_service = DockerService()

        # Pull and run container with Traefik labels
        container_id = await docker_service.deploy(
            image=request.image,
            container_name=container_name,
            path_prefix=deployment_id,
            port=request.port,
            env=request.env,
        )

        # Store deployment info
        deployment = Deployment(
            id=deployment_id,
            path_prefix=deployment_id,
            image=request.image,
            port=request.port,
            url=url,
            container_id=container_id,
            status="running",
            created_at=datetime.now(timezone.utc),
        )
        _deployments[deployment_id] = deployment

        logger.info("deployment_successful", deployment_id=deployment_id, url=url)

        return DeployResponse(
            status="deployed",
            deployment_id=deployment_id,
            url=url,
            container_id=container_id,
        )

    except Exception as e:
        logger.error("deployment_failed", error=str(e))
        return DeployResponse(
            status="failed",
            deployment_id=deployment_id,
            url=url,
            error=str(e),
        )


@router.delete("/webhook/deploy/{deployment_id}")
async def teardown_container(
    deployment_id: str,
    x_sandbox_secret: str | None = Header(None, alias="X-Sandbox-Secret"),
):
    """Teardown a deployed container."""
    _verify_secret(x_sandbox_secret)

    container_name = f"{settings.CONTAINER_PREFIX}-{deployment_id}"

    logger.info("tearing_down_container", deployment_id=deployment_id)

    try:
        docker_service = DockerService()
        await docker_service.teardown(container_name)

        # Remove from storage
        if deployment_id in _deployments:
            del _deployments[deployment_id]

        return {"status": "removed", "deployment_id": deployment_id}

    except Exception as e:
        logger.error("teardown_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/deployments")
async def list_deployments():
    """List all active deployments."""
    docker_service = DockerService()
    containers = await docker_service.list_sandbox_containers()

    return {
        "deployments": list(_deployments.values()),
        "containers": containers,
    }


@router.get("/deployments/{deployment_id}", response_model=EnhancedDeployment)
async def get_deployment(deployment_id: str) -> EnhancedDeployment:
    """Get enhanced deployment details with runtime information."""
    if deployment_id not in _deployments:
        raise HTTPException(status_code=404, detail="Deployment not found")

    deployment = _deployments[deployment_id]
    container_name = f"{settings.CONTAINER_PREFIX}-{deployment_id}"

    # Get container details
    docker_service = DockerService()
    container_info = await _get_container_info(docker_service, container_name)

    # Calculate uptime
    uptime_seconds = (datetime.now(timezone.utc) - deployment.created_at).total_seconds()

    return EnhancedDeployment(
        id=deployment.id,
        path_prefix=deployment.path_prefix,
        image=deployment.image,
        port=deployment.port,
        url=deployment.url,
        container_id=deployment.container_id,
        status=deployment.status,
        created_at=deployment.created_at,
        ttl_minutes=deployment.ttl_minutes,
        container_state=container_info.get("state", "unknown"),
        health_status=container_info.get("health", "unknown"),
        uptime_seconds=uptime_seconds,
        last_health_check=datetime.now(timezone.utc),
        resource_usage=container_info.get("resources"),
        logs_url=f"/api/deployments/{deployment_id}/logs",
        artifacts_url=f"/api/artifacts?deployment_id={deployment_id}",
        metrics_url="/api/metrics",
        websocket_url=f"/ws/progress/{deployment_id}",
    )


async def _get_container_info(docker_service: DockerService, container_name: str) -> dict[str, Any]:
    """Get detailed container information."""
    import asyncio

    import docker
    from docker.errors import NotFound

    def _get_info():
        try:
            client = docker.from_env()
            container = client.containers.get(container_name)

            # Get stats (non-streaming for single snapshot)
            stats = container.stats(stream=False)

            # Calculate CPU percentage
            cpu_delta = stats["cpu_stats"]["cpu_usage"]["total_usage"] - \
                        stats["precpu_stats"]["cpu_usage"]["total_usage"]
            system_delta = stats["cpu_stats"]["system_cpu_usage"] - \
                           stats["precpu_stats"]["system_cpu_usage"]
            cpu_percent = 0.0
            if system_delta > 0:
                cpu_percent = (cpu_delta / system_delta) * 100.0

            # Memory usage
            memory_usage = stats["memory_stats"].get("usage", 0)
            memory_limit = stats["memory_stats"].get("limit", 0)
            memory_percent = (memory_usage / memory_limit * 100) if memory_limit > 0 else 0.0

            # Health status
            health = "unknown"
            if hasattr(container, "attrs") and "State" in container.attrs:
                state_info = container.attrs["State"]
                if "Health" in state_info:
                    health = state_info["Health"].get("Status", "unknown")
                elif state_info.get("Running"):
                    health = "running"
                else:
                    health = "stopped"

            return {
                "state": container.status,
                "health": health,
                "resources": ResourceUsage(
                    cpu_percent=round(cpu_percent, 2),
                    memory_bytes=memory_usage,
                    memory_limit_bytes=memory_limit,
                    memory_percent=round(memory_percent, 2),
                ),
            }
        except NotFound:
            return {"state": "not_found", "health": "unknown", "resources": None}
        except Exception as e:
            logger.warning("container_info_error", container=container_name, error=str(e))
            return {"state": "error", "health": "unknown", "resources": None}

    return await asyncio.to_thread(_get_info)
