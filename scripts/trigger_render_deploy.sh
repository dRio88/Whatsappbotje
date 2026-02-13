#!/bin/bash

# Render Deploy Trigger Script
# Zet je Render deploy hook URL hier

RENDER_DEPLOY_HOOK_URL="${RENDER_DEPLOY_HOOK_URL:-}"

if [ -z "$RENDER_DEPLOY_HOOK_URL" ]; then
    echo "Error: RENDER_DEPLOY_HOOK_URL environment variable is not set"
    exit 1
fi

echo "Triggering Render deployment..."
curl -X POST -f "$RENDER_DEPLOY_HOOK_URL"

if [ $? -eq 0 ]; then
    echo "Deployment triggered successfully!"
    exit 0
else
    echo "Failed to trigger deployment"
    exit 1
fi
