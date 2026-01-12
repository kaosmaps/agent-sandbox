"""Deployment webhook receiver and management API."""

import secrets
from datetime import datetime, timezone

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


class DeployResponse(BaseModel):
    """Deployment response."""

    status: str
    deployment_id: str
    url: str
    container_id: str | None = None
    error: str | None = None


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


@router.get("/deployments/{deployment_id}")
async def get_deployment(deployment_id: str):
    """Get deployment details."""
    if deployment_id not in _deployments:
        raise HTTPException(status_code=404, detail="Deployment not found")

    return _deployments[deployment_id]
