# Agent Sandbox API

Webhook receiver for NanoSwarm agent container deployments.

## Development

```bash
# Install dependencies
uv pip install -e .

# Run locally
uvicorn app.main:app --reload
```

## Endpoints

- `GET /health` - Health check
- `POST /api/webhook/deploy` - Deploy a container
- `DELETE /api/webhook/deploy/{id}` - Teardown a container
- `GET /api/deployments` - List deployments
