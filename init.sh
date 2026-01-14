#!/bin/bash
# Agent Sandbox Development Server Initialization
# Used by autonomous mode to start the dev environment

set -e

PROJECT_DIR="/Users/jankothyson/Code/nano/agent-sandbox"
cd "$PROJECT_DIR"

echo "=== Agent Sandbox Init ==="
echo "Project: $PROJECT_DIR"
echo "Time: $(date +"%Y-%m-%d %H:%M:%S")"

# Ensure we have the actual repo (not skeleton)
if [ ! -d ".git" ]; then
  echo "ERROR: Not a git repo. Run setup first."
  exit 1
fi

# Check for existing server
if [ -f ".server.pid" ]; then
  OLD_PID=$(cat .server.pid)
  if ps -p "$OLD_PID" > /dev/null 2>&1; then
    echo "Stopping existing server (PID: $OLD_PID)"
    kill "$OLD_PID" 2>/dev/null || true
    sleep 2
  fi
  rm -f .server.pid
fi

# Change to API directory
cd apps/api

# Create virtual environment if needed
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

# Activate virtual environment
source .venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -e ".[dev]" --quiet

# Create data directories
mkdir -p /tmp/agent-sandbox/artifacts
mkdir -p /tmp/agent-sandbox/data

# Export environment
export DATA_DIR="/tmp/agent-sandbox/data"
export ARTIFACTS_DIR="/tmp/agent-sandbox/artifacts"
export DEBUG=true
export APP_VERSION="dev"

# Return to project root
cd "$PROJECT_DIR"

# Start development server
echo "Starting development server..."
cd apps/api
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload &
SERVER_PID=$!
cd "$PROJECT_DIR"

echo $SERVER_PID > .server.pid
echo "Server PID: $SERVER_PID"

# Wait for health
echo "Waiting for server to be healthy..."
for i in {1..30}; do
  if curl -s http://localhost:8080/health > /dev/null 2>&1; then
    echo "=== Server ready at http://localhost:8080 ==="
    echo "Health: http://localhost:8080/health"
    echo "Docs: http://localhost:8080/docs"
    exit 0
  fi
  echo "  Waiting... ($i/30)"
  sleep 1
done

echo "ERROR: Server failed to start within 30 seconds"
cat .server.pid 2>/dev/null && kill $(cat .server.pid) 2>/dev/null
exit 1
