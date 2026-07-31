#!/bin/sh
set -eu

ESBERN_API_TOKEN=$(/bin/cat "${CREDENTIALS_DIRECTORY:?}/api-token")
export ESBERN_API_TOKEN

exec /opt/esbern/.venv/bin/esbern-mcp
