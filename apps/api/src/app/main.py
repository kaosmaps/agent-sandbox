"""Agent Sandbox API main application."""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import artifacts, deployments, health, logs, metrics
from app.ws import progress
from app.core.config import settings

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer() if settings.DEBUG else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info("starting_agent_sandbox_api", version=settings.APP_VERSION)
    yield
    logger.info("shutting_down_agent_sandbox_api")


app = FastAPI(
    title="Agent Sandbox API",
    description="Webhook receiver for NanoSwarm agent container deployments",
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health.router, tags=["health"])
app.include_router(deployments.router, prefix="/api", tags=["deployments"])
app.include_router(artifacts.router, prefix="/api", tags=["artifacts"])
app.include_router(logs.router, prefix="/api", tags=["logs"])
app.include_router(metrics.router, prefix="/api", tags=["metrics"])
app.include_router(progress.router, tags=["websocket"])


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "Agent Sandbox API",
        "version": settings.APP_VERSION,
        "docs": "/docs",
    }
