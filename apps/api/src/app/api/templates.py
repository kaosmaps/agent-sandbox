"""Template registry and instantiation API.

Provides endpoints for listing, viewing, and instantiating deployment templates.
Templates are pre-configured project scaffolds that can be customized and deployed.
"""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

logger = structlog.get_logger()
router = APIRouter()

# Templates directory (relative to project root)
TEMPLATES_DIR = Path(__file__).parent.parent.parent.parent.parent.parent / "templates"


class TemplateVariable(BaseModel):
    """A variable that can be substituted in a template."""

    name: str
    description: str = ""
    default: str | None = None
    required: bool = False


class TemplateFile(BaseModel):
    """A file in a template."""

    path: str
    size: int
    is_binary: bool = False


class TemplateInfo(BaseModel):
    """Template metadata."""

    name: str
    description: str = ""
    files: list[TemplateFile]
    variables: list[TemplateVariable]
    dockerfile: bool = False
    port: int = 3000


class TemplateListResponse(BaseModel):
    """Response for listing templates."""

    templates: list[dict[str, Any]]
    count: int


class InstantiateRequest(BaseModel):
    """Request to instantiate a template."""

    name: str = Field(..., description="Name for the instantiated project")
    variables: dict[str, str] = Field(default_factory=dict, description="Variable substitutions")
    deployment_id: str | None = Field(None, description="Deployment ID to associate with")


class InstantiateResponse(BaseModel):
    """Response after instantiating a template."""

    status: str
    name: str
    files: list[str]
    output_dir: str
    deployment_id: str | None = None


def _get_template_metadata(template_path: Path) -> dict[str, Any]:
    """Read template.json metadata if it exists."""
    metadata_path = template_path / "template.json"
    if metadata_path.exists():
        with open(metadata_path) as f:
            return json.load(f)
    return {}


def _scan_template_files(template_path: Path) -> list[TemplateFile]:
    """Scan template directory for files."""
    files = []
    for root, _, filenames in os.walk(template_path):
        for filename in filenames:
            if filename == "template.json":
                continue  # Skip metadata file

            file_path = Path(root) / filename
            relative_path = file_path.relative_to(template_path)

            # Check if binary (simple heuristic)
            is_binary = False
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    f.read(1024)
            except UnicodeDecodeError:
                is_binary = True

            files.append(TemplateFile(
                path=str(relative_path),
                size=file_path.stat().st_size,
                is_binary=is_binary,
            ))

    return files


def _extract_variables(template_path: Path) -> list[TemplateVariable]:
    """Extract variables from template files.

    Looks for {{VARIABLE_NAME}} patterns in text files.
    """
    variables: dict[str, TemplateVariable] = {}

    for root, _, filenames in os.walk(template_path):
        for filename in filenames:
            file_path = Path(root) / filename

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()

                # Find all {{VARIABLE_NAME}} patterns
                matches = re.findall(r"\{\{(\w+)\}\}", content)
                for match in matches:
                    if match not in variables:
                        variables[match] = TemplateVariable(
                            name=match,
                            description=f"Variable: {match}",
                        )

            except (UnicodeDecodeError, IOError):
                continue  # Skip binary files

    return list(variables.values())


def _get_template_info(template_name: str) -> TemplateInfo:
    """Get detailed information about a template."""
    template_path = TEMPLATES_DIR / template_name

    if not template_path.exists() or not template_path.is_dir():
        raise HTTPException(status_code=404, detail=f"Template '{template_name}' not found")

    metadata = _get_template_metadata(template_path)
    files = _scan_template_files(template_path)
    variables = _extract_variables(template_path)

    # Merge with metadata variables if present
    if "variables" in metadata:
        for var_data in metadata["variables"]:
            existing = next((v for v in variables if v.name == var_data["name"]), None)
            if existing:
                existing.description = var_data.get("description", existing.description)
                existing.default = var_data.get("default", existing.default)
                existing.required = var_data.get("required", existing.required)
            else:
                variables.append(TemplateVariable(**var_data))

    return TemplateInfo(
        name=template_name,
        description=metadata.get("description", f"{template_name} template"),
        files=files,
        variables=variables,
        dockerfile=(template_path / "Dockerfile").exists(),
        port=metadata.get("port", 3000),
    )


@router.get("/templates", response_model=TemplateListResponse)
async def list_templates():
    """List all available templates.

    Returns template names, descriptions, and file counts.
    """
    templates = []

    if not TEMPLATES_DIR.exists():
        logger.warning("templates_dir_not_found", path=str(TEMPLATES_DIR))
        return TemplateListResponse(templates=[], count=0)

    for entry in TEMPLATES_DIR.iterdir():
        if entry.is_dir() and not entry.name.startswith("."):
            try:
                info = _get_template_info(entry.name)
                templates.append({
                    "name": info.name,
                    "description": info.description,
                    "file_count": len(info.files),
                    "has_dockerfile": info.dockerfile,
                    "port": info.port,
                    "variables": [v.name for v in info.variables],
                })
            except Exception as e:
                logger.warning("template_scan_error", template=entry.name, error=str(e))

    return TemplateListResponse(templates=templates, count=len(templates))


@router.get("/templates/{template_name}")
async def get_template(template_name: str) -> TemplateInfo:
    """Get detailed information about a template.

    Returns file list, variables, and configuration.
    """
    return _get_template_info(template_name)


@router.get("/templates/{template_name}/files/{file_path:path}")
async def get_template_file(
    template_name: str,
    file_path: str,
):
    """Get contents of a template file."""
    template_path = TEMPLATES_DIR / template_name
    full_path = template_path / file_path

    if not template_path.exists():
        raise HTTPException(status_code=404, detail=f"Template '{template_name}' not found")

    if not full_path.exists():
        raise HTTPException(status_code=404, detail=f"File '{file_path}' not found")

    # Security: ensure we're not escaping the template directory
    try:
        full_path.resolve().relative_to(template_path.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file path")

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()

        return {
            "path": file_path,
            "content": content,
            "size": full_path.stat().st_size,
        }
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Cannot read binary file as text")


@router.post("/templates/{template_name}/instantiate", response_model=InstantiateResponse)
async def instantiate_template(
    template_name: str,
    request: InstantiateRequest,
):
    """Instantiate a template with variable substitutions.

    Creates a copy of the template with all {{VARIABLE}} placeholders replaced.
    """
    template_path = TEMPLATES_DIR / template_name

    if not template_path.exists():
        raise HTTPException(status_code=404, detail=f"Template '{template_name}' not found")

    # Validate required variables
    info = _get_template_info(template_name)
    missing = []
    for var in info.variables:
        if var.required and var.name not in request.variables:
            if var.default is None:
                missing.append(var.name)

    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required variables: {', '.join(missing)}",
        )

    # Create output directory
    output_dir = Path(tempfile.mkdtemp(prefix=f"sandbox-{request.name}-"))

    # Build substitution map with defaults
    substitutions = {var.name: var.default or "" for var in info.variables}
    substitutions.update(request.variables)

    # Also add PROJECT_NAME variable
    substitutions["PROJECT_NAME"] = request.name

    logger.info(
        "instantiating_template",
        template=template_name,
        name=request.name,
        variables=list(substitutions.keys()),
    )

    output_files = []

    for root, dirs, filenames in os.walk(template_path):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        rel_root = Path(root).relative_to(template_path)
        output_root = output_dir / rel_root
        output_root.mkdir(parents=True, exist_ok=True)

        for filename in filenames:
            if filename == "template.json":
                continue  # Skip metadata

            src_path = Path(root) / filename
            dst_path = output_root / filename

            # Try to read as text and substitute
            try:
                with open(src_path, "r", encoding="utf-8") as f:
                    content = f.read()

                # Substitute variables
                for var_name, var_value in substitutions.items():
                    content = content.replace(f"{{{{{var_name}}}}}", var_value)

                with open(dst_path, "w", encoding="utf-8") as f:
                    f.write(content)

            except UnicodeDecodeError:
                # Binary file, just copy
                shutil.copy2(src_path, dst_path)

            output_files.append(str(dst_path.relative_to(output_dir)))

    logger.info(
        "template_instantiated",
        template=template_name,
        name=request.name,
        output_dir=str(output_dir),
        file_count=len(output_files),
    )

    return InstantiateResponse(
        status="instantiated",
        name=request.name,
        files=output_files,
        output_dir=str(output_dir),
        deployment_id=request.deployment_id,
    )


@router.delete("/templates/instances/{instance_dir:path}")
async def delete_instance(instance_dir: str):
    """Delete an instantiated template directory.

    Used for cleanup after deployment.
    """
    instance_path = Path(instance_dir)

    # Security: only allow deleting from temp directory
    if not str(instance_path).startswith(tempfile.gettempdir()):
        raise HTTPException(status_code=400, detail="Can only delete temporary instances")

    if not instance_path.exists():
        raise HTTPException(status_code=404, detail="Instance not found")

    shutil.rmtree(instance_path)

    return {"status": "deleted", "path": str(instance_path)}
