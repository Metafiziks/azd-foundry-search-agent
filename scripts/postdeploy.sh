#!/usr/bin/env bash
set -euo pipefail

echo ""
echo "=== Post-Deploy: Granting search permissions to agent ==="
echo ""

SEARCH_RESOURCE_ID="${AZURE_SEARCH_RESOURCE_ID:-}"

if [ -z "$SEARCH_RESOURCE_ID" ]; then
  echo "⚠  AZURE_SEARCH_RESOURCE_ID not set. Re-run 'azd provision' first."
  exit 0
fi

# Invoke agent synchronously — this triggers container startup which logs the OID.
# The invoke itself takes ~25-40s; after it returns, logs will have the OID.
echo "► Starting agent to capture hosting identity..."
azd ai agent invoke search-agent '{"messages":[{"role":"user","content":"startup"}]}' > /dev/null 2>&1 || true

# Give logs a moment to flush, then capture
sleep 5
LOGS=$(azd ai agent monitor search-agent --tail 100 2>&1 || true)

# Extract OID — note \1 back-reference to capture group
HOSTING_OID=$(echo "$LOGS" | grep "IDENTITY OID" | sed 's/.*IDENTITY OID: \([a-f0-9-]*\) .*/\1/' | head -1 || true)

if [ -z "$HOSTING_OID" ]; then
  echo ""
  echo "⚠  Could not auto-detect hosting identity OID."
  echo "   Grant the role manually after running the agent once:"
  echo "     OID=\$(azd ai agent monitor search-agent --tail 30 | grep 'IDENTITY OID' | grep -oE '[0-9a-f-]{36}' | head -1)"
  echo "     az role assignment create --assignee \"\$OID\" --role 'Search Index Data Reader' --scope ${SEARCH_RESOURCE_ID}"
  echo ""
  exit 0
fi

echo "  Hosting identity: ${HOSTING_OID}"
echo "► Granting Search Index Data Reader..."

az role assignment create \
  --assignee "$HOSTING_OID" \
  --role "Search Index Data Reader" \
  --scope "$SEARCH_RESOURCE_ID" \
  --output none 2>/dev/null && echo "  ✓ Permission granted" || echo "  (role already assigned)"

# RBAC assignments can take up to 5 minutes to propagate globally.
# We wait 2 minutes which covers most cases.
echo ""
echo "  Waiting 2 minutes for RBAC propagation..."
sleep 120

echo "► Verifying agent search..."
azd ai agent invoke search-agent '{"messages":[{"role":"user","content":"What documents are available?"}]}' 2>&1 \
  | grep -v "^Session\|^Conversation\|^Trace\|^Next\|^azd\|^Set up\|^WARNING\|^\s*$" \
  | head -15 || true

echo ""
echo "=== Post-Deploy Complete ==="
echo ""
