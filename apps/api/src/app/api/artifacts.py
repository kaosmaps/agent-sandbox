"""Artifact management API.

Endpoints for uploading, downloading, and managing deployment artifacts.
Artifacts are files produced by agent containers that can be persisted
and optionally committed back to git repositories.
"""

import mimetypes
from typing import Annotated

import structlog
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.services.storage import ArtifactMetadata, storage_service

logger = structlog.get_logger()
router = APIRouter()


class ArtifactUploadResponse(BaseModel):
    """Response after uploading an artifact."""

    id: str
    deployment_id: str
    filename: str
    content_type: str
    size: int
    sha256: str
    url: str


class ArtifactListResponse(BaseModel):
    """Response for listing artifacts."""

    artifacts: list[dict]
    count: int


class GitCommitRequest(BaseModel):
    """Request to commit artifacts to git."""

    deployment_id: str = Field(..., description="Deployment to commit artifacts from")
    repo: str = Field(..., description="Target repository (e.g., kaosmaps/my-repo)")
    branch: str = Field(default="main", description="Base branch for the commit")
    message: str = Field(default="Agent artifacts", description="Commit message")
    create_pr: bool = Field(default=False, description="Create a pull request")


class GitCommitResponse(BaseModel):
    """Response after committing artifacts."""

    status: str
    commit_sha: str | None = None
    commit_url: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    error: str | None = None


def _metadata_to_response(metadata: ArtifactMetadata) -> ArtifactUploadResponse:
    """Convert metadata to API response."""
    return ArtifactUploadResponse(
        id=metadata.id,
        deployment_id=metadata.deployment_id,
        filename=metadata.filename,
        content_type=metadata.content_type,
        size=metadata.size,
        sha256=metadata.sha256,
        url=f"/api/artifacts/{metadata.id}",
    )


@router.post("/artifacts/upload", response_model=ArtifactUploadResponse)
async def upload_artifact(
    file: Annotated[UploadFile, File(description="File to upload")],
    deployment_id: Annotated[str, Form(description="Deployment ID")],
):
    """Upload an artifact file.

    Stores the file in persistent storage and tracks metadata.
    Returns artifact details including download URL.
    """
    # Read file content
    content = await file.read()

    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    # Determine content type
    content_type = file.content_type
    if not content_type or content_type == "application/octet-stream":
        guessed_type, _ = mimetypes.guess_type(file.filename or "unknown")
        content_type = guessed_type or "application/octet-stream"

    logger.info(
        "uploading_artifact",
        deployment_id=deployment_id,
        filename=file.filename,
        size=len(content),
        content_type=content_type,
    )

    try:
        metadata = await storage_service.save_artifact(
            deployment_id=deployment_id,
            filename=file.filename or "unnamed",
            content=content,
            content_type=content_type,
        )

        return _metadata_to_response(metadata)

    except Exception as e:
        logger.error("artifact_upload_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")


@router.get("/artifacts/{artifact_id}")
async def download_artifact(artifact_id: str):
    """Download an artifact by ID.

    Returns the file with appropriate Content-Type and Content-Disposition headers.
    """
    result = await storage_service.get_artifact(artifact_id)

    if result is None:
        raise HTTPException(status_code=404, detail="Artifact not found")

    metadata, content = result

    return Response(
        content=content,
        media_type=metadata.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{metadata.filename}"',
            "X-Artifact-ID": metadata.id,
            "X-Artifact-SHA256": metadata.sha256,
        },
    )


@router.get("/artifacts/{artifact_id}/metadata")
async def get_artifact_metadata(artifact_id: str) -> dict:
    """Get artifact metadata without downloading content."""
    result = await storage_service.get_artifact(artifact_id)

    if result is None:
        raise HTTPException(status_code=404, detail="Artifact not found")

    metadata, _ = result
    return metadata.to_dict()


@router.get("/artifacts", response_model=ArtifactListResponse)
async def list_artifacts(
    deployment_id: Annotated[str | None, Query(description="Filter by deployment")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """List artifacts, optionally filtered by deployment.

    Returns artifact metadata without file content.
    """
    artifacts = await storage_service.list_artifacts(
        deployment_id=deployment_id,
        limit=limit,
        offset=offset,
    )

    return ArtifactListResponse(
        artifacts=[a.to_dict() for a in artifacts],
        count=len(artifacts),
    )


@router.delete("/artifacts/{artifact_id}")
async def delete_artifact(artifact_id: str):
    """Delete an artifact."""
    deleted = await storage_service.delete_artifact(artifact_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Artifact not found")

    return {"status": "deleted", "artifact_id": artifact_id}


@router.delete("/deployments/{deployment_id}/artifacts")
async def delete_deployment_artifacts(deployment_id: str):
    """Delete all artifacts for a deployment."""
    count = await storage_service.delete_deployment_artifacts(deployment_id)

    return {
        "status": "deleted",
        "deployment_id": deployment_id,
        "count": count,
    }


@router.post("/artifacts/commit", response_model=GitCommitResponse)
async def commit_artifacts(request: GitCommitRequest):
    """Commit deployment artifacts to a git repository.

    Creates a new branch with the artifacts and optionally opens a PR.
    Requires GITHUB_TOKEN to be configured.
    """
    # Import here to avoid circular imports
    from app.services.git_artifacts import git_service

    logger.info(
        "committing_artifacts",
        deployment_id=request.deployment_id,
        repo=request.repo,
        branch=request.branch,
    )

    try:
        result = await git_service.commit_artifacts(
            deployment_id=request.deployment_id,
            repo=request.repo,
            base_branch=request.branch,
            message=request.message,
            create_pr=request.create_pr,
        )

        return GitCommitResponse(
            status="committed",
            commit_sha=result.get("sha"),
            commit_url=result.get("commit_url"),
            branch=result.get("branch"),
            pr_url=result.get("pr_url"),
        )

    except ValueError as e:
        # Expected errors (no artifacts, missing config, etc.)
        logger.warning("artifact_commit_failed", error=str(e))
        return GitCommitResponse(status="failed", error=str(e))

    except Exception as e:
        logger.error("artifact_commit_error", error=str(e))
        return GitCommitResponse(status="error", error=str(e))
