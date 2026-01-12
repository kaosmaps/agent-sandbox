# Agent Sandbox

Multi-tenant container deployment platform for NanoSwarm agents.

## Overview

Agent Sandbox provides a secure environment where KAI agents can deploy their work products to isolated URL paths. Each deployment gets its own path prefix (e.g., `/task-abc123/`) with automatic SSL and Traefik routing.

## Architecture

```
api.sandbox.nanoswarm.kaosmaps.com
├── /                    → Sandbox API (health, docs, deployments list)
├── /api/webhook/deploy  → Deployment trigger endpoint
├── /api/deployments     → Deployment management
├── /task-abc123/        → Deployed container A
├── /task-def456/        → Deployed container B
└── /task-ghi789/        → Deployed container C
```

## Quick Start

### For Agents

See [CLAUDE.md](CLAUDE.md) for agent deployment instructions.

### For Operators

```bash
# Clone repository
git clone https://github.com/kaosmaps/agent-sandbox.git
cd agent-sandbox

# Setup environment
cp .env.example .env
# Edit .env with your secrets

# Deploy with Docker Compose
docker compose -f docker/docker-compose.yml up -d
```

## Configuration

| Environment Variable | Description | Required |
|---------------------|-------------|----------|
| `WEBHOOK_SECRET` | Secret for webhook authentication | Yes |
| `GHCR_TOKEN` | GitHub Container Registry token | Yes |
| `DEBUG` | Enable debug mode | No |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/api/webhook/deploy` | POST | Deploy a container |
| `/api/webhook/deploy/{id}` | DELETE | Teardown a container |
| `/api/deployments` | GET | List all deployments |

## Security

- Webhook endpoints protected by `X-Sandbox-Secret` header
- All traffic uses HTTPS via Let's Encrypt
- Containers run in isolated Docker network
- No direct internet access from containers

## License

MIT
