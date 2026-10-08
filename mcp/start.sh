#!/bin/sh
# Launches the brain MCP server with whatever Node is installed (nvm versions move).
NODE="$(command -v node 2>/dev/null)"
[ -x "$NODE" ] || NODE="$(ls -d "$HOME"/.nvm/versions/node/*/bin/node 2>/dev/null | sort -V | tail -1)"
[ -x "$NODE" ] || NODE=/opt/homebrew/bin/node
exec "$NODE" "$(dirname "$0")/server.mjs"
