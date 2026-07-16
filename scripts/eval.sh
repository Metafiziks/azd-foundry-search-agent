#!/usr/bin/env bash
# eval.sh — run the evaluation suite against the deployed agent
# Usage: bash scripts/eval.sh [--no-judge]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo ""
echo "=== Azure AI Foundry Agent Evaluation ==="
echo ""

# Load azd env vars if FOUNDRY_PROJECT_ENDPOINT isn't already set
if [ -z "${FOUNDRY_PROJECT_ENDPOINT:-}" ]; then
  if command -v azd &>/dev/null; then
    echo "► Loading environment from azd..."
    set -a; eval "$(azd env get-values 2>/dev/null)"; set +a
  fi
fi

if [ -z "${FOUNDRY_PROJECT_ENDPOINT:-}" ]; then
  echo "ERROR: FOUNDRY_PROJECT_ENDPOINT is not set."
  echo "  Run 'azd up' first, then re-run this script."
  exit 1
fi

# Install dependencies into a local venv if needed
VENV_DIR="${REPO_ROOT}/.venv-eval"
if [ ! -f "${VENV_DIR}/bin/python3" ]; then
  echo "► Creating eval virtualenv..."
  python3 -m venv "${VENV_DIR}"
fi
"${VENV_DIR}/bin/pip" install azure-identity -q

echo ""
"${VENV_DIR}/bin/python3" "${SCRIPT_DIR}/run_evals.py" \
  --output "${REPO_ROOT}/eval_results.json" \
  "$@"
