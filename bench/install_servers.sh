#!/usr/bin/env bash
# Builds the Go-based MCP servers used by the benchmark into bench/bin.
# The npx/uvx-based servers are fetched on first use and need no install.
# Requires Go 1.24+ and git.
set -euo pipefail
cd "$(dirname "$0")"
export GOBIN="$PWD/bin"
mkdir -p "$GOBIN"

go install github.com/github/github-mcp-server/cmd/github-mcp-server@latest
go install github.com/grafana/mcp-grafana/cmd/mcp-grafana@latest
go install github.com/hashicorp/terraform-mcp-server/cmd/terraform-mcp-server@latest

# These two use replace directives, so `go install` refuses them; build from source.
tmp="$(mktemp -d)"
for repo in mcp-victorialogs mcp-victoriametrics; do
  git clone -q --depth 1 "https://github.com/VictoriaMetrics/$repo" "$tmp/$repo"
  (cd "$tmp/$repo" && go build -o "$GOBIN/$repo" "./cmd/$repo")
done
rm -rf "$tmp"
ls "$GOBIN"
