#!/bin/sh
set -eu

ESBERN_API_TOKEN=$(/bin/cat "${CREDENTIALS_DIRECTORY:?}/api-token")
GOOGLE_BOOKS_API_KEY=$(/bin/cat "${CREDENTIALS_DIRECTORY:?}/google-books-api-key")
ESBERN_ANT_API_KEY=$(/bin/cat "${CREDENTIALS_DIRECTORY:?}/anthropic-api-key")
export ESBERN_API_TOKEN GOOGLE_BOOKS_API_KEY ESBERN_ANT_API_KEY

exec /opt/esbern/.venv/bin/esbern serve \
  --path /srv/esbern/reading \
  --host 127.0.0.1 \
  --port 8037
