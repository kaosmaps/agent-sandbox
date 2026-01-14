"""{{PROJECT_NAME}} - Main FastAPI application."""

import logging
import sys
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Configure JSON logging
logging.basicConfig(
    format='{"time":"%(asctime)s","level":"%(levelname)s","message":"%(message)s"}',
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


app = FastAPI(
    title="{{PROJECT_NAME}}",
    description="Agent-generated FastAPI application",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "{{PROJECT_NAME}}",
        "version": "0.1.0",
        "docs": "/docs",
    }


@app.get("/health")
async def health():
    """Health check endpoint for container orchestration."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.on_event("startup")
async def startup_event():
    """Log startup."""
    logger.info("Application starting up")


@app.on_event("shutdown")
async def shutdown_event():
    """Log shutdown."""
    logger.info("Application shutting down")
