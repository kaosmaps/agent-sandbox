"""Container log streaming API.

Provides endpoints for retrieving and streaming container logs.
Supports both one-shot retrieval and Server-Sent Events (SSE) streaming.
"""

import asyncio
from typing import Annotated, AsyncGenerator

import structlog
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.services.docker import DockerService

logger = structlog.get_logger()
router = APIRouter()


@router.get("/deployments/{deployment_id}/logs")
async def get_container_logs(
    deployment_id: str,
    tail: Annotated[int, Query(ge=1, le=10000, description="Number of lines to return")] = 100,
    follow: Annotated[bool, Query(description="Stream logs in real-time via SSE")] = False,
    timestamps: Annotated[bool, Query(description="Include timestamps")] = True,
):
    """Get container logs for a deployment.

    If follow=true, returns a Server-Sent Events stream for real-time logs.
    Otherwise, returns the last N lines of logs as plain text.
    """
    from app.core.config import settings

    container_name = f"{settings.CONTAINER_PREFIX}-{deployment_id}"

    docker_service = DockerService()

    if follow:
        # Return SSE stream
        return StreamingResponse(
            _stream_logs(docker_service, container_name, timestamps),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        # Return static logs
        try:
            logs = await docker_service.get_container_logs(container_name, tail=tail)
            return {
                "deployment_id": deployment_id,
                "container": container_name,
                "lines": tail,
                "logs": logs,
            }
        except Exception as e:
            logger.error("log_retrieval_failed", deployment_id=deployment_id, error=str(e))
            raise HTTPException(status_code=500, detail=f"Failed to get logs: {e}")


async def _stream_logs(
    docker_service: DockerService,
    container_name: str,
    timestamps: bool,
) -> AsyncGenerator[str, None]:
    """Stream container logs as SSE events.

    Yields:
        SSE-formatted log lines
    """
    import docker
    from docker.errors import NotFound

    try:
        # Get container
        client = docker.from_env()
        container = client.containers.get(container_name)

        # Stream logs
        log_stream = container.logs(
            stream=True,
            follow=True,
            timestamps=timestamps,
            tail=50,  # Start with last 50 lines
        )

        for line in log_stream:
            decoded = line.decode("utf-8").strip()
            if decoded:
                # Format as SSE event
                yield f"data: {decoded}\n\n"
            # Small delay to prevent overwhelming
            await asyncio.sleep(0.01)

    except NotFound:
        yield f"event: error\ndata: Container {container_name} not found\n\n"
    except Exception as e:
        logger.error("log_stream_error", container=container_name, error=str(e))
        yield f"event: error\ndata: {str(e)}\n\n"
    finally:
        yield "event: close\ndata: Stream ended\n\n"


@router.get("/deployments/{deployment_id}/logs/download")
async def download_container_logs(
    deployment_id: str,
    tail: Annotated[int, Query(ge=1, le=100000)] = 10000,
):
    """Download full container logs as a text file."""
    from app.core.config import settings

    container_name = f"{settings.CONTAINER_PREFIX}-{deployment_id}"

    docker_service = DockerService()

    try:
        logs = await docker_service.get_container_logs(container_name, tail=tail)

        return StreamingResponse(
            iter([logs]),
            media_type="text/plain",
            headers={
                "Content-Disposition": f'attachment; filename="{deployment_id}-logs.txt"',
            },
        )
    except Exception as e:
        logger.error("log_download_failed", deployment_id=deployment_id, error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to download logs: {e}")
