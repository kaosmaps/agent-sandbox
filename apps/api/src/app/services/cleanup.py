"""Container cleanup service.

Provides automatic cleanup of expired containers based on TTL,
and removal of orphaned containers.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog

from app.core.config import settings
from app.services.docker import DockerService

logger = structlog.get_logger()


@dataclass
class CleanupResult:
    """Result of a cleanup operation."""

    expired_count: int
    orphan_count: int
    failed_count: int
    containers_removed: list[str]
    errors: list[str]


class CleanupService:
    """Service for automatic container cleanup."""

    def __init__(
        self,
        check_interval_seconds: float = 300.0,  # 5 minutes
        default_ttl_minutes: int = 60,
    ):
        self.check_interval = check_interval_seconds
        self.default_ttl = default_ttl_minutes
        self._running = False
        self._task: asyncio.Task | None = None
        # deployment_id -> (created_at, ttl_minutes)
        self._deployments: dict[str, tuple[datetime, int]] = {}

    def register_deployment(
        self,
        deployment_id: str,
        created_at: datetime,
        ttl_minutes: int | None = None,
    ) -> None:
        """Register a deployment for TTL tracking.

        Args:
            deployment_id: Deployment identifier
            created_at: When the deployment was created
            ttl_minutes: Time-to-live in minutes (0 = no expiry)
        """
        ttl = ttl_minutes if ttl_minutes is not None else self.default_ttl
        self._deployments[deployment_id] = (created_at, ttl)

        logger.info(
            "deployment_registered_for_cleanup",
            deployment_id=deployment_id,
            ttl_minutes=ttl,
            expires_at=(
                (created_at.replace(tzinfo=timezone.utc) +
                 __import__("datetime").timedelta(minutes=ttl)).isoformat()
                if ttl > 0 else "never"
            ),
        )

    def unregister_deployment(self, deployment_id: str) -> bool:
        """Remove a deployment from TTL tracking.

        Args:
            deployment_id: Deployment identifier

        Returns:
            True if deployment was registered, False otherwise
        """
        if deployment_id in self._deployments:
            del self._deployments[deployment_id]
            logger.info("deployment_unregistered_from_cleanup", deployment_id=deployment_id)
            return True
        return False

    def get_expired_deployments(self) -> list[str]:
        """Get list of deployments that have exceeded their TTL."""
        now = datetime.now(timezone.utc)
        expired = []

        for deployment_id, (created_at, ttl_minutes) in self._deployments.items():
            if ttl_minutes == 0:
                continue  # No expiry

            # Ensure created_at is timezone-aware
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            age_minutes = (now - created_at).total_seconds() / 60

            if age_minutes >= ttl_minutes:
                expired.append(deployment_id)

        return expired

    async def cleanup_expired(self) -> CleanupResult:
        """Clean up expired containers.

        Returns:
            CleanupResult with details of the operation
        """
        docker_service = DockerService()
        expired = self.get_expired_deployments()

        result = CleanupResult(
            expired_count=0,
            orphan_count=0,
            failed_count=0,
            containers_removed=[],
            errors=[],
        )

        if not expired:
            logger.debug("no_expired_containers")
            return result

        logger.info("cleaning_up_expired_containers", count=len(expired))

        for deployment_id in expired:
            container_name = f"{settings.CONTAINER_PREFIX}-{deployment_id}"

            try:
                await docker_service.teardown(container_name)
                self.unregister_deployment(deployment_id)
                result.expired_count += 1
                result.containers_removed.append(container_name)

                logger.info(
                    "expired_container_removed",
                    deployment_id=deployment_id,
                    container_name=container_name,
                )

            except Exception as e:
                result.failed_count += 1
                result.errors.append(f"{container_name}: {e}")
                logger.error(
                    "expired_container_removal_failed",
                    deployment_id=deployment_id,
                    error=str(e),
                )

        return result

    async def cleanup_orphans(self) -> CleanupResult:
        """Clean up orphaned containers (running but not tracked).

        Returns:
            CleanupResult with details of the operation
        """
        docker_service = DockerService()

        result = CleanupResult(
            expired_count=0,
            orphan_count=0,
            failed_count=0,
            containers_removed=[],
            errors=[],
        )

        try:
            containers = await docker_service.list_sandbox_containers()
        except Exception as e:
            logger.error("failed_to_list_containers", error=str(e))
            result.errors.append(f"List failed: {e}")
            return result

        # Find orphans (containers not in our tracking)
        tracked_ids = set(self._deployments.keys())

        for container in containers:
            container_name = container.get("name", "")
            # Extract deployment_id from container name
            if container_name.startswith(f"{settings.CONTAINER_PREFIX}-"):
                deployment_id = container_name[len(settings.CONTAINER_PREFIX) + 1:]

                if deployment_id not in tracked_ids:
                    logger.warning(
                        "found_orphan_container",
                        container_name=container_name,
                        deployment_id=deployment_id,
                    )

                    try:
                        await docker_service.teardown(container_name)
                        result.orphan_count += 1
                        result.containers_removed.append(container_name)

                        logger.info(
                            "orphan_container_removed",
                            container_name=container_name,
                        )

                    except Exception as e:
                        result.failed_count += 1
                        result.errors.append(f"{container_name}: {e}")

        return result

    async def run_cleanup(self) -> CleanupResult:
        """Run full cleanup (expired + orphans).

        Returns:
            Combined CleanupResult
        """
        logger.info("running_cleanup_cycle")

        expired_result = await self.cleanup_expired()
        orphan_result = await self.cleanup_orphans()

        combined = CleanupResult(
            expired_count=expired_result.expired_count,
            orphan_count=orphan_result.orphan_count,
            failed_count=expired_result.failed_count + orphan_result.failed_count,
            containers_removed=(
                expired_result.containers_removed + orphan_result.containers_removed
            ),
            errors=expired_result.errors + orphan_result.errors,
        )

        logger.info(
            "cleanup_cycle_complete",
            expired=combined.expired_count,
            orphans=combined.orphan_count,
            failed=combined.failed_count,
            total_removed=len(combined.containers_removed),
        )

        return combined

    async def start_background_cleanup(self) -> None:
        """Start the background cleanup task."""
        if self._running:
            logger.warning("cleanup_service_already_running")
            return

        self._running = True
        self._task = asyncio.create_task(self._cleanup_loop())
        logger.info(
            "cleanup_service_started",
            interval_seconds=self.check_interval,
        )

    async def stop_background_cleanup(self) -> None:
        """Stop the background cleanup task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("cleanup_service_stopped")

    async def _cleanup_loop(self) -> None:
        """Background cleanup loop."""
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
                await self.run_cleanup()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("cleanup_loop_error", error=str(e))

    def get_status(self) -> dict[str, Any]:
        """Get cleanup service status."""
        return {
            "running": self._running,
            "check_interval_seconds": self.check_interval,
            "default_ttl_minutes": self.default_ttl,
            "tracked_deployments": len(self._deployments),
            "expired_count": len(self.get_expired_deployments()),
        }

    def get_deployment_ttl_info(self, deployment_id: str) -> dict[str, Any] | None:
        """Get TTL info for a specific deployment."""
        if deployment_id not in self._deployments:
            return None

        created_at, ttl_minutes = self._deployments[deployment_id]

        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        age_minutes = (now - created_at).total_seconds() / 60

        return {
            "deployment_id": deployment_id,
            "created_at": created_at.isoformat(),
            "ttl_minutes": ttl_minutes,
            "age_minutes": round(age_minutes, 2),
            "remaining_minutes": max(0, round(ttl_minutes - age_minutes, 2)) if ttl_minutes > 0 else None,
            "expired": age_minutes >= ttl_minutes if ttl_minutes > 0 else False,
        }


# Global instance
cleanup_service = CleanupService()
