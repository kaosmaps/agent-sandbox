"""Docker container management service."""

import asyncio
from dataclasses import dataclass
from typing import Any

import docker
import structlog
from docker.errors import DockerException, NotFound

from app.core.config import settings

logger = structlog.get_logger()


@dataclass
class ResourceLimits:
    """Container resource limits."""

    memory_mb: int = 512  # Memory limit in MB
    cpu_count: float = 0.5  # CPU cores (0.5 = half a core)
    pids_limit: int = 100  # Max number of processes


@dataclass
class HealthCheckConfig:
    """Container health check configuration."""

    enabled: bool = True
    path: str = "/health"
    port: int | None = None  # Use container port if not specified
    interval_seconds: int = 30
    timeout_seconds: int = 10
    retries: int = 3
    start_period_seconds: int = 10


class DockerService:
    """Service for managing Docker containers."""

    # Default resource limits
    DEFAULT_LIMITS = ResourceLimits()

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
        limits: ResourceLimits | None = None,
        health_check: HealthCheckConfig | None = None,
    ) -> str:
        """Deploy a container with Traefik labels for routing.

        Args:
            image: Docker image to deploy
            container_name: Name for the container
            path_prefix: URL path prefix for routing
            port: Container port to expose
            env: Environment variables
            limits: Resource limits (memory, CPU)
            health_check: Health check configuration

        Returns:
            Container ID
        """
        # Use defaults if not specified
        limits = limits or self.DEFAULT_LIMITS
        health_check = health_check or HealthCheckConfig(port=port)

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
            "sandbox.memory_limit_mb": str(limits.memory_mb),
            "sandbox.cpu_limit": str(limits.cpu_count),
        }

        # Build health check command
        healthcheck = None
        if health_check.enabled:
            check_port = health_check.port or port
            healthcheck = {
                "test": ["CMD", "curl", "-f", f"http://localhost:{check_port}{health_check.path}"],
                "interval": health_check.interval_seconds * 1_000_000_000,  # nanoseconds
                "timeout": health_check.timeout_seconds * 1_000_000_000,
                "retries": health_check.retries,
                "start_period": health_check.start_period_seconds * 1_000_000_000,
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

            # Create and start container with resource limits
            logger.info(
                "creating_container",
                name=container_name,
                network=settings.DOCKER_NETWORK,
                memory_mb=limits.memory_mb,
                cpu_count=limits.cpu_count,
            )

            container = self.client.containers.run(
                image=image,
                name=container_name,
                detach=True,
                network=settings.DOCKER_NETWORK,
                labels=labels,
                environment=env or {},
                restart_policy={"Name": "unless-stopped"},
                # Resource limits (F021)
                mem_limit=f"{limits.memory_mb}m",
                nano_cpus=int(limits.cpu_count * 1_000_000_000),  # CPU in nanoseconds
                pids_limit=limits.pids_limit,
                # Health check (F023)
                healthcheck=healthcheck,
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

    async def get_container_health(self, container_name: str) -> dict[str, Any]:
        """Get container health status and history (F023).

        Returns:
            Health information including status, log, and history
        """

        def _health():
            try:
                container = self.client.containers.get(container_name)
                attrs = container.attrs

                state = attrs.get("State", {})
                health = state.get("Health", {})

                # Get health check log (last 10 entries)
                log = health.get("Log", [])[-10:]

                return {
                    "status": health.get("Status", "unknown"),
                    "failing_streak": health.get("FailingStreak", 0),
                    "log": [
                        {
                            "start": entry.get("Start"),
                            "end": entry.get("End"),
                            "exit_code": entry.get("ExitCode"),
                            "output": entry.get("Output", "")[:500],  # Truncate
                        }
                        for entry in log
                    ],
                    "container_status": state.get("Status", "unknown"),
                    "running": state.get("Running", False),
                    "started_at": state.get("StartedAt"),
                    "finished_at": state.get("FinishedAt"),
                }

            except NotFound:
                return {
                    "status": "not_found",
                    "failing_streak": 0,
                    "log": [],
                    "container_status": "not_found",
                    "running": False,
                }
            except Exception as e:
                logger.error("health_check_error", container=container_name, error=str(e))
                return {
                    "status": "error",
                    "error": str(e),
                    "failing_streak": 0,
                    "log": [],
                    "container_status": "error",
                    "running": False,
                }

        return await asyncio.to_thread(_health)

    async def get_container_stats(self, container_name: str) -> dict[str, Any]:
        """Get container resource usage statistics.

        Returns:
            CPU, memory, and network stats
        """

        def _stats():
            try:
                container = self.client.containers.get(container_name)
                stats = container.stats(stream=False)

                # Calculate CPU percentage
                cpu_delta = (
                    stats["cpu_stats"]["cpu_usage"]["total_usage"]
                    - stats["precpu_stats"]["cpu_usage"]["total_usage"]
                )
                system_delta = (
                    stats["cpu_stats"]["system_cpu_usage"]
                    - stats["precpu_stats"]["system_cpu_usage"]
                )
                cpu_percent = 0.0
                if system_delta > 0:
                    cpu_percent = (cpu_delta / system_delta) * 100.0

                # Memory stats
                memory = stats.get("memory_stats", {})
                memory_usage = memory.get("usage", 0)
                memory_limit = memory.get("limit", 0)

                # Network stats
                networks = stats.get("networks", {})
                network_rx = sum(n.get("rx_bytes", 0) for n in networks.values())
                network_tx = sum(n.get("tx_bytes", 0) for n in networks.values())

                return {
                    "cpu_percent": round(cpu_percent, 2),
                    "memory_bytes": memory_usage,
                    "memory_limit_bytes": memory_limit,
                    "memory_percent": round(
                        (memory_usage / memory_limit * 100) if memory_limit > 0 else 0, 2
                    ),
                    "network_rx_bytes": network_rx,
                    "network_tx_bytes": network_tx,
                    "pids_current": stats.get("pids_stats", {}).get("current", 0),
                }

            except NotFound:
                return None
            except Exception as e:
                logger.error("stats_error", container=container_name, error=str(e))
                return None

        return await asyncio.to_thread(_stats)
