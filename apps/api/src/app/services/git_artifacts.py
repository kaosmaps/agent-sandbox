"""Git artifact commit service.

Provides functionality to commit deployment artifacts to git repositories.
Uses GitPython for git operations and GitHub API for PR creation.
"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx
import structlog
from git import Repo
from git.exc import GitCommandError

from app.core.config import settings
from app.services.storage import storage_service

logger = structlog.get_logger()


class GitArtifactService:
    """Service for committing artifacts to git repositories."""

    def __init__(self):
        """Initialize git service."""
        self.github_token = settings.GITHUB_TOKEN
        self.git_user_name = settings.GIT_USER_NAME
        self.git_user_email = settings.GIT_USER_EMAIL

    async def commit_artifacts(
        self,
        deployment_id: str,
        repo: str,
        base_branch: str = "main",
        message: str = "Agent artifacts",
        create_pr: bool = False,
    ) -> dict[str, Any]:
        """Commit deployment artifacts to a git repository.

        Args:
            deployment_id: ID of the deployment with artifacts
            repo: Target repository (e.g., "kaosmaps/my-repo")
            base_branch: Base branch to create from
            message: Commit message
            create_pr: Whether to create a pull request

        Returns:
            Dictionary with commit details (sha, url, branch, pr_url)
        """
        if not self.github_token:
            raise ValueError("GITHUB_TOKEN not configured")

        # Get artifacts for this deployment
        artifacts = await storage_service.list_artifacts(deployment_id=deployment_id)

        if not artifacts:
            raise ValueError(f"No artifacts found for deployment {deployment_id}")

        logger.info(
            "preparing_git_commit",
            deployment_id=deployment_id,
            repo=repo,
            artifact_count=len(artifacts),
        )

        # Create a unique branch name
        branch_name = f"agent/{deployment_id}"

        # Run git operations in thread pool
        result = await asyncio.to_thread(
            self._do_git_operations,
            artifacts=artifacts,
            repo=repo,
            base_branch=base_branch,
            branch_name=branch_name,
            message=message,
        )

        # Create PR if requested
        if create_pr and result.get("sha"):
            pr_result = await self._create_pull_request(
                repo=repo,
                head=branch_name,
                base=base_branch,
                title=f"Agent artifacts: {deployment_id}",
                body=f"Artifacts from agent deployment `{deployment_id}`.\n\n"
                f"Commit: {result['sha']}\n"
                f"Files: {len(artifacts)}",
            )
            result["pr_url"] = pr_result.get("url")
            result["pr_number"] = pr_result.get("number")

        return result

    def _do_git_operations(
        self,
        artifacts: list,
        repo: str,
        base_branch: str,
        branch_name: str,
        message: str,
    ) -> dict[str, Any]:
        """Perform git operations (runs in thread pool).

        This is synchronous code that runs in a thread.
        """
        repo_url = f"https://x-access-token:{self.github_token}@github.com/{repo}.git"

        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)

            # Clone the repository (shallow clone for speed)
            logger.info("cloning_repository", repo=repo, branch=base_branch)
            try:
                git_repo = Repo.clone_from(
                    repo_url,
                    work_dir,
                    branch=base_branch,
                    depth=1,
                    single_branch=True,
                )
            except GitCommandError as e:
                logger.error("clone_failed", error=str(e))
                raise ValueError(f"Failed to clone repository: {e}")

            # Configure git user
            git_repo.config_writer().set_value("user", "name", self.git_user_name).release()
            git_repo.config_writer().set_value("user", "email", self.git_user_email).release()

            # Create new branch
            logger.info("creating_branch", branch=branch_name)
            try:
                git_repo.create_head(branch_name)
                git_repo.heads[branch_name].checkout()
            except Exception as e:
                logger.error("branch_creation_failed", error=str(e))
                raise ValueError(f"Failed to create branch: {e}")

            # Create artifacts directory in repo
            artifacts_dir = work_dir / "artifacts"
            artifacts_dir.mkdir(exist_ok=True)

            # Copy artifacts
            for artifact in artifacts:
                src_path = Path(artifact.path)
                if src_path.exists():
                    dst_path = artifacts_dir / artifact.filename
                    shutil.copy2(src_path, dst_path)
                    logger.debug("copied_artifact", filename=artifact.filename)

            # Stage all changes
            git_repo.index.add(["artifacts"])

            # Check if there are changes to commit
            if not git_repo.index.diff("HEAD"):
                logger.warning("no_changes_to_commit")
                raise ValueError("No changes to commit")

            # Commit
            commit = git_repo.index.commit(message)
            commit_sha = commit.hexsha

            logger.info("committed_artifacts", sha=commit_sha)

            # Push to remote
            logger.info("pushing_to_remote", branch=branch_name)
            try:
                origin = git_repo.remote("origin")
                origin.push(refspec=f"{branch_name}:{branch_name}", force=True)
            except GitCommandError as e:
                logger.error("push_failed", error=str(e))
                raise ValueError(f"Failed to push: {e}")

            return {
                "sha": commit_sha,
                "branch": branch_name,
                "commit_url": f"https://github.com/{repo}/commit/{commit_sha}",
            }

    async def _create_pull_request(
        self,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        """Create a GitHub pull request.

        Args:
            repo: Repository (e.g., "kaosmaps/my-repo")
            head: Source branch
            base: Target branch
            title: PR title
            body: PR body

        Returns:
            Dictionary with PR details (url, number)
        """
        url = f"https://api.github.com/repos/{repo}/pulls"

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.github_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={
                    "title": title,
                    "body": body,
                    "head": head,
                    "base": base,
                },
            )

            if response.status_code == 201:
                data = response.json()
                logger.info("pull_request_created", number=data["number"], url=data["html_url"])
                return {
                    "url": data["html_url"],
                    "number": data["number"],
                }
            elif response.status_code == 422:
                # PR might already exist
                logger.warning("pull_request_exists_or_invalid", status=response.status_code)
                return {"url": None, "number": None}
            else:
                logger.error(
                    "pull_request_creation_failed",
                    status=response.status_code,
                    body=response.text,
                )
                return {"url": None, "number": None}

    async def get_repository_info(self, repo: str) -> dict[str, Any] | None:
        """Get information about a GitHub repository.

        Args:
            repo: Repository (e.g., "kaosmaps/my-repo")

        Returns:
            Repository info or None if not found
        """
        if not self.github_token:
            return None

        url = f"https://api.github.com/repos/{repo}"

        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {self.github_token}",
                    "Accept": "application/vnd.github+json",
                },
            )

            if response.status_code == 200:
                return response.json()
            return None


# Global instance for dependency injection
git_service = GitArtifactService()
