"""Artifact storage service for managing deployment outputs.

Provides persistent storage for artifacts produced by agent containers.
Uses SQLite for metadata and filesystem for file content.
"""

import asyncio
import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

import aiosqlite
import structlog

from app.core.config import settings

logger = structlog.get_logger()


@dataclass
class ArtifactMetadata:
    """Metadata for a stored artifact."""

    id: str
    deployment_id: str
    filename: str
    content_type: str
    size: int
    sha256: str
    created_at: datetime
    path: str

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "size": self.size,
            "sha256": self.sha256,
            "created_at": self.created_at.isoformat(),
            "url": f"/api/artifacts/{self.id}",
        }


class StorageService:
    """Service for storing and retrieving deployment artifacts.

    Artifacts are stored on the filesystem organized by deployment_id,
    with metadata tracked in SQLite for querying and integrity verification.
    """

    def __init__(self, artifacts_dir: str | None = None, db_path: str | None = None):
        """Initialize storage service.

        Args:
            artifacts_dir: Directory for storing artifact files
            db_path: Path to SQLite database for metadata
        """
        self.artifacts_dir = Path(artifacts_dir or settings.ARTIFACTS_DIR)
        self.db_path = db_path or settings.ARTIFACTS_DB
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize storage - create directories and database schema."""
        if self._initialized:
            return

        # Create artifacts directory
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        logger.info("artifacts_dir_created", path=str(self.artifacts_dir))

        # Create database schema
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    path TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_artifacts_deployment
                ON artifacts(deployment_id)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_artifacts_sha256
                ON artifacts(sha256)
            """)
            await db.commit()
            logger.info("artifacts_db_initialized", path=self.db_path)

        self._initialized = True

    async def save_artifact(
        self,
        deployment_id: str,
        filename: str,
        content: bytes | BinaryIO,
        content_type: str = "application/octet-stream",
    ) -> ArtifactMetadata:
        """Save an artifact to storage.

        Args:
            deployment_id: ID of the deployment that produced this artifact
            filename: Original filename
            content: File content (bytes or file-like object)
            content_type: MIME type of the content

        Returns:
            ArtifactMetadata with storage details
        """
        await self.initialize()

        # Generate unique ID
        artifact_id = str(uuid.uuid4())

        # Read content if it's a file-like object
        if hasattr(content, "read"):
            content = content.read()

        # Calculate hash and size
        sha256 = hashlib.sha256(content).hexdigest()
        size = len(content)

        # Create deployment directory
        deployment_dir = self.artifacts_dir / deployment_id
        deployment_dir.mkdir(parents=True, exist_ok=True)

        # Save file (use artifact_id to avoid collisions)
        safe_filename = f"{artifact_id}_{filename}"
        file_path = deployment_dir / safe_filename

        # Write in thread to avoid blocking
        def _write():
            with open(file_path, "wb") as f:
                f.write(content)

        await asyncio.to_thread(_write)

        # Create metadata
        now = datetime.utcnow()
        metadata = ArtifactMetadata(
            id=artifact_id,
            deployment_id=deployment_id,
            filename=filename,
            content_type=content_type,
            size=size,
            sha256=sha256,
            created_at=now,
            path=str(file_path),
        )

        # Store in database
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO artifacts
                (id, deployment_id, filename, content_type, size, sha256, created_at, path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metadata.id,
                    metadata.deployment_id,
                    metadata.filename,
                    metadata.content_type,
                    metadata.size,
                    metadata.sha256,
                    metadata.created_at.isoformat(),
                    metadata.path,
                ),
            )
            await db.commit()

        logger.info(
            "artifact_saved",
            artifact_id=artifact_id,
            deployment_id=deployment_id,
            filename=filename,
            size=size,
            sha256=sha256[:16] + "...",
        )

        return metadata

    async def get_artifact(self, artifact_id: str) -> tuple[ArtifactMetadata, bytes] | None:
        """Retrieve an artifact by ID.

        Args:
            artifact_id: Unique artifact identifier

        Returns:
            Tuple of (metadata, content) or None if not found
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            logger.warning("artifact_not_found", artifact_id=artifact_id)
            return None

        metadata = ArtifactMetadata(
            id=row["id"],
            deployment_id=row["deployment_id"],
            filename=row["filename"],
            content_type=row["content_type"],
            size=row["size"],
            sha256=row["sha256"],
            created_at=datetime.fromisoformat(row["created_at"]),
            path=row["path"],
        )

        # Read file content
        file_path = Path(metadata.path)
        if not file_path.exists():
            logger.error("artifact_file_missing", artifact_id=artifact_id, path=metadata.path)
            return None

        def _read():
            with open(file_path, "rb") as f:
                return f.read()

        content = await asyncio.to_thread(_read)

        # Verify integrity
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != metadata.sha256:
            logger.error(
                "artifact_integrity_error",
                artifact_id=artifact_id,
                expected=metadata.sha256,
                actual=actual_hash,
            )
            return None

        return metadata, content

    async def list_artifacts(
        self, deployment_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[ArtifactMetadata]:
        """List artifacts, optionally filtered by deployment.

        Args:
            deployment_id: Optional filter by deployment
            limit: Maximum number of results
            offset: Pagination offset

        Returns:
            List of artifact metadata
        """
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if deployment_id:
                query = """
                    SELECT * FROM artifacts
                    WHERE deployment_id = ?
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                """
                params = (deployment_id, limit, offset)
            else:
                query = """
                    SELECT * FROM artifacts
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                """
                params = (limit, offset)

            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()

        return [
            ArtifactMetadata(
                id=row["id"],
                deployment_id=row["deployment_id"],
                filename=row["filename"],
                content_type=row["content_type"],
                size=row["size"],
                sha256=row["sha256"],
                created_at=datetime.fromisoformat(row["created_at"]),
                path=row["path"],
            )
            for row in rows
        ]

    async def delete_artifact(self, artifact_id: str) -> bool:
        """Delete an artifact.

        Args:
            artifact_id: Unique artifact identifier

        Returns:
            True if deleted, False if not found
        """
        await self.initialize()

        # Get metadata first to find file path
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT path FROM artifacts WHERE id = ?", (artifact_id,)
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            return False

        # Delete file
        file_path = Path(row["path"])
        if file_path.exists():

            def _delete():
                os.remove(file_path)

            await asyncio.to_thread(_delete)

        # Delete from database
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
            await db.commit()

        logger.info("artifact_deleted", artifact_id=artifact_id)
        return True

    async def delete_deployment_artifacts(self, deployment_id: str) -> int:
        """Delete all artifacts for a deployment.

        Args:
            deployment_id: Deployment identifier

        Returns:
            Number of artifacts deleted
        """
        await self.initialize()

        # Get all artifacts for this deployment
        artifacts = await self.list_artifacts(deployment_id=deployment_id, limit=10000)

        # Delete each one
        count = 0
        for artifact in artifacts:
            if await self.delete_artifact(artifact.id):
                count += 1

        # Clean up empty deployment directory
        deployment_dir = self.artifacts_dir / deployment_id
        if deployment_dir.exists() and not any(deployment_dir.iterdir()):
            deployment_dir.rmdir()

        logger.info(
            "deployment_artifacts_deleted",
            deployment_id=deployment_id,
            count=count,
        )

        return count


# Global instance for dependency injection
storage_service = StorageService()
