# Agent Sandbox Instructions

**Last Updated**: 2026-01-12 23:45

This repository is the default workspace for NanoSwarm agents to build and deploy applications.

---

## Deployment Protocol

When your task produces a deployable application, follow these steps:

### 1. Build Docker Image

```bash
# Get task ID (first 12 chars for path prefix)
TASK_PREFIX="${TASK_ID:0:12}"

# Build the image
docker build -t ghcr.io/kaosmaps/agent-sandbox:task-${TASK_PREFIX} .

# Push to GitHub Container Registry
docker push ghcr.io/kaosmaps/agent-sandbox:task-${TASK_PREFIX}
```

### 2. Trigger Deployment

```bash
curl -X POST https://api.nanoswarm.kaosmaps.com/api/sandbox/deploy \
  -H "Authorization: Bearer $DEPLOY_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "'$TASK_ID'",
    "image": "ghcr.io/kaosmaps/agent-sandbox:task-'${TASK_PREFIX}'",
    "port": 3000
  }'
```

### 3. Report URL

Your deployment will be available at:
```
https://api.sandbox.nanoswarm.kaosmaps.com/${TASK_PREFIX}/
```

Include this URL in your task completion report.

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `TASK_ID` | Current task UUID |
| `DEPLOY_KEY` | Your `dpk_*` deployment key |
| `GITHUB_REPO` | This repository (`kaosmaps/agent-sandbox`) |
| `GHCR_TOKEN` | GitHub Container Registry token |

---

## Templates

Use these templates as starting points:

| Template | Description | Use When |
|----------|-------------|----------|
| `templates/react-vite/` | React + Vite + TypeScript | Frontend-only apps |
| `templates/fastapi/` | FastAPI backend | API-only apps |
| `templates/fullstack/` | React + FastAPI monorepo | Full applications |

### Quick Start

```bash
# Copy template to working directory
cp -r templates/react-vite/* .

# Install dependencies
bun install

# Build for production
bun run build

# Create Dockerfile (see templates for examples)
```

---

## Best Practices

1. **Always use multi-stage Docker builds** to minimize image size
2. **Set proper health checks** in your Dockerfile
3. **Use environment variables** for configuration (never hardcode)
4. **Test locally first** before deploying
5. **Include a README.md** explaining what you built

---

## Direct Deployment (Alternative)

If you have SSH access to the sandbox server:

```bash
# SSH to sandbox server
ssh -i ~/.ssh/kaosmaps-sandbox-deploy root@91.99.51.1

# Deploy directly via docker
docker run -d \
  --name sandbox-${TASK_PREFIX} \
  --network sandbox-network \
  -l "traefik.enable=true" \
  -l "traefik.http.routers.sandbox-${TASK_PREFIX}.rule=Host(\`api.sandbox.nanoswarm.kaosmaps.com\`) && PathPrefix(\`/${TASK_PREFIX}\`)" \
  -l "traefik.http.routers.sandbox-${TASK_PREFIX}.entrypoints=websecure" \
  -l "traefik.http.routers.sandbox-${TASK_PREFIX}.tls.certresolver=letsencrypt" \
  -l "traefik.http.services.sandbox-${TASK_PREFIX}.loadbalancer.server.port=3000" \
  -l "traefik.http.middlewares.sandbox-${TASK_PREFIX}-strip.stripprefix.prefixes=/${TASK_PREFIX}" \
  -l "traefik.http.routers.sandbox-${TASK_PREFIX}.middlewares=sandbox-${TASK_PREFIX}-strip" \
  ghcr.io/kaosmaps/agent-sandbox:task-${TASK_PREFIX}
```

---

## Troubleshooting

### Container not accessible

1. Check if container is running: `docker ps | grep sandbox`
2. Check container logs: `docker logs sandbox-${TASK_PREFIX}`
3. Verify Traefik labels: `docker inspect sandbox-${TASK_PREFIX} | jq '.[0].Config.Labels'`

### SSL certificate issues

- Wait 1-2 minutes for Let's Encrypt to issue certificate
- Check Traefik logs: `docker logs sandbox-traefik`

### Image pull fails

- Ensure GHCR_TOKEN is set and valid
- Check image exists: `docker pull ghcr.io/kaosmaps/agent-sandbox:task-${TASK_PREFIX}`
