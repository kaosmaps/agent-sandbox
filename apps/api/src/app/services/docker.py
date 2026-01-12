"""Docker container management service."""

import asyncio
from typing import Any

import docker
import structlog
from docker.errors import DockerException, NotFound

from app.core.config import settings

logger = structlog.get_logger()


class DockerService:
    """Service for managing Docker containers."""

    def __init__(self):
        """Initialize Docker client."""
        self.client = docker.from_env()

    async def deploy(
        self,
        image: str,
        container_name: str,
        path_prefix: str,
        port: int,
        env: dict[str, str] | None = None,
    ) -> str:
        """Deploy a container with Traefik labels for routing.

        Returns:
            Container ID
        """
        # Build Traefik labels for path-based routing
        labels = {
            "traefik.enable": "true",
            f"traefik.http.routers.{container_name}.rule": (
                f"Host(`{settings.SANDBOX_DOMAIN}`) && PathPrefix(`/{path_prefix}`)"
            ),
            f"traefik.http.routers.{container_name}.entrypoints": "websecure",
            f"traefik.http.routers.{container_name}.tls.certresolver": "letsencrypt",
            f"traefik.http.services.{container_name}.loadbalancer.server.port": str(port),
            f"traefik.http.middlewares.{container_name}-strip.stripprefix.prefixes": f"/{path_prefix}",
            f"traefik.http.routers.{container_name}.middlewares": f"{container_name}-strip",
            # Sandbox metadata
            "sandbox.deployment": "true",
            "sandbox.path_prefix": path_prefix,
        }

        # Run in thread pool since docker-py is sync
        def _deploy():
            # Pull image
            logger.info("pulling_image", image=image)
            try:
                self.client.images.pull(image)
            except DockerException as e:
                logger.warning("image_pull_warning", error=str(e))

            # Remove existing container if any
            try:
                existing = self.client.containers.get(container_name)
                logger.info("removing_existing_container", name=container_name)
                existing.remove(force=True)
            except NotFound:
                pass

            # Create and start container
            logger.info("creating_container", name=container_name, network=settings.DOCKER_NETWORK)
            container = self.client.containers.run(
                image=image,
                name=container_name,
                detach=True,
                network=settings.DOCKER_NETWORK,
                labels=labels,
                environment=env or {},
                restart_policy={"Name": "unless-stopped"},
            )
            return container.short_id

        return await asyncio.to_thread(_deploy)

    async def teardown(self, container_name: str) -> None:
        """Remove a container."""

        def _teardown():
            try:
                container = self.client.containers.get(container_name)
                container.remove(force=True)
                logger.info("container_removed", name=container_name)
            except NotFound:
                logger.warning("container_not_found", name=container_name)

        await asyncio.to_thread(_teardown)

    async def list_sandbox_containers(self) -> list[dict[str, Any]]:
        """List all sandbox containers."""

        def _list():
            containers = self.client.containers.list(
                filters={"label": "sandbox.deployment=true"}
            )
            return [
                {
                    "id": c.short_id,
                    "name": c.name,
                    "status": c.status,
                    "image": c.image.tags[0] if c.image.tags else "unknown",
                    "path_prefix": c.labels.get("sandbox.path_prefix", ""),
                }
                for c in containers
            ]

        return await asyncio.to_thread(_list)

    async def get_container_logs(self, container_name: str, tail: int = 100) -> str:
        """Get container logs."""

        def _logs():
            try:
                container = self.client.containers.get(container_name)
                return container.logs(tail=tail).decode("utf-8")
            except NotFound:
                return ""

        return await asyncio.to_thread(_logs)
