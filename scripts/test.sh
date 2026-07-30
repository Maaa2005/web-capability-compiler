#!/usr/bin/env bash
# Lint, type-check, and test. Phase 3 wires this same script into CI.
set -euo pipefail

cd "$(dirname "$0")/.."

uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
