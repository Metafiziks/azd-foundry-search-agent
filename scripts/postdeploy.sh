#!/usr/bin/env bash
set -euo pipefail

echo ""
echo "=== Post-Deploy: Granting search permissions to agent ==="
echo ""

SEARCH_RESOURCE_ID="${AZURE_SEARCH_RESOURCE_ID:-}"
RG="${AZURE_RESOURCE_GROUP:-}"

if [ -z "$SEARCH_RESOURCE_ID" ] || [ -z "$RG" ]; then
  echo "⚠  Required environment variables not set. Re-run 'azd provision' first."
  exit 0
fi

# Foundry creates a user-assigned managed identity for the agent using this naming pattern:
#   {cogservice}-{env_name}-{agent_name}-AgentIdentity
# Look it up directly from Azure AD by display name (faster and more reliable than log scraping).
echo "► Locating agent hosting identity..."

COG_ACCOUNT=$(az cognitiveservices account list   --resource-group "$RG"   --query "[0].name" -o tsv 2>/dev/null || true)

HOSTING_OID=""
if [ -n "$COG_ACCOUNT" ]; then
  AGENT_IDENTITY_NAME="${COG_ACCOUNT}-${AZURE_ENV_NAME}-search-agent-AgentIdentity"
  echo "  Looking for: ${AGENT_IDENTITY_NAME}"
  HOSTING_OID=$(az ad sp list     --display-name "$AGENT_IDENTITY_NAME"     --query "[0].id" -o tsv 2>/dev/null || true)
fi

# Fallback: invoke the agent and scan recent logs for the OID
if [ -z "$HOSTING_OID" ]; then
  echo "  Identity not found by name — invoking agent to capture OID from logs..."
  for attempt in 1 2 3; do
    echo "  Attempt ${attempt}/3..."
    azd ai agent invoke search-agent       '{"messages":[{"role":"user","content":"What documents are in the knowledge base?"}]}'       > /dev/null 2>&1 || true
    sleep 10
  done
  LOGS=$(azd ai agent monitor search-agent --tail 200 2>&1 || true)
  HOSTING_OID=$(echo "$LOGS" | grep "IDENTITY OID"     | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'     | head -1 || true)
fi

if [ -n "$HOSTING_OID" ]; then
  echo "  Hosting identity: ${HOSTING_OID}"
  echo "► Granting Search Index Data Reader..."
  az role assignment create     --assignee "$HOSTING_OID"     --role "Search Index Data Reader"     --scope "$SEARCH_RESOURCE_ID"     --output none 2>/dev/null     && echo "  ✓ Permission granted"     || echo "  (role already assigned)"
else
  echo ""
  echo "⚠  Could not auto-detect hosting identity OID."
  echo "   Grant the role manually after running the agent once:"
  echo ""
  echo "     OID=\$(azd ai agent monitor search-agent --tail 50 2>&1 | grep 'IDENTITY OID' | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)"
  echo "     az role assignment create --assignee \"\$OID\" --role 'Search Index Data Reader' --scope ${SEARCH_RESOURCE_ID}"
  echo ""
fi

# RBAC assignments propagate globally over 2–10 minutes.
# The hub and project identities were already granted in postprovision (earlier in the pipeline),
# so this wait covers the agent identity grant above.
echo ""
echo "  Waiting 5 minutes for RBAC propagation..."
sleep 300

# --- Generate eval cases from docs + run evaluations ---
echo ""
echo "► Generating eval cases from docs/..."
VENV_EVAL="${HOME}/.azd-eval-venv"
[ -f "${VENV_EVAL}/bin/python3" ] || python3 -m venv "${VENV_EVAL}"
"${VENV_EVAL}/bin/pip" install azure-identity -q

eval "$(azd env get-values 2>/dev/null)" || true

FOUNDRY_PROJECT_ENDPOINT="${FOUNDRY_PROJECT_ENDPOINT:-}" \
AZURE_AI_MODEL_DEPLOYMENT_NAME="${AZURE_AI_MODEL_DEPLOYMENT_NAME:-gpt-5}" \
  "${VENV_EVAL}/bin/python3" "$(dirname "$0")/generate_eval_cases.py" || true
echo ""

echo "► Running automated evaluations..."
FOUNDRY_PROJECT_ENDPOINT="${FOUNDRY_PROJECT_ENDPOINT:-}" \
AZURE_AI_MODEL_DEPLOYMENT_NAME="${AZURE_AI_MODEL_DEPLOYMENT_NAME:-gpt-5}" \
  "${VENV_EVAL}/bin/python3" "$(dirname "$0")/run_evals.py" \
    --output "$(dirname "$(dirname "$0")")/eval_results.json" || true
echo ""

echo ""
echo "=== Post-Deploy Complete ==="
echo ""
echo "► Test your agent in the Azure AI Foundry portal (recommended — fresh session each time):"
echo "  https://ai.azure.com"
echo ""
echo "  Or invoke from the CLI:"
echo "  azd ai agent invoke search-agent '{\"messages\":[{\"role\":\"user\",\"content\":\"What documents are available?\"}]}'"
echo "  Note: the CLI reuses conversation history across invokes. If you see stale"
echo "  responses, test via the portal instead."
echo ""
