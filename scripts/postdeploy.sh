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

# Invoke the agent with a question that triggers search_knowledge_base.
# That function logs the hosting identity OID on first call.
# Run up to 3 times to handle cold starts (container may need time to initialize).
echo "► Invoking agent to capture hosting identity (triggers search + OID log)..."
for attempt in 1 2 3; do
  echo "  Attempt ${attempt}/3..."
  azd ai agent invoke search-agent \
    '{"messages":[{"role":"user","content":"What documents are in the knowledge base?"}]}' \
    > /dev/null 2>&1 || true
  sleep 5
done

# Extract OID from recent container logs using a precise UUID pattern
LOGS=$(azd ai agent monitor search-agent --tail 200 2>&1 || true)
HOSTING_OID=$(echo "$LOGS" | grep "IDENTITY OID" \
  | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
  | head -1 || true)

if [ -z "$HOSTING_OID" ]; then
  echo ""
  echo "⚠  Could not auto-detect hosting identity OID."
  echo "   Run the agent manually once, then grant the role:"
  echo ""
  echo "     azd ai agent invoke search-agent '{\"messages\":[{\"role\":\"user\",\"content\":\"What documents are available?\"}]}'"
  echo "     OID=\$(azd ai agent monitor search-agent --tail 50 2>&1 | grep 'IDENTITY OID' | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)"
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
  --output none 2>/dev/null \
  && echo "  ✓ Permission granted" \
  || echo "  (role already assigned)"

# RBAC assignments can take up to 5 minutes to propagate globally.
echo ""
echo "  Waiting 3 minutes for RBAC propagation..."
sleep 180

echo "► Verifying agent search..."
azd ai agent invoke search-agent \
  '{"messages":[{"role":"user","content":"What are the steps for lockout/tagout?"}]}' \
  2>&1 | grep -v "^Session\|^Conversation\|^Trace\|^Next\|^azd\|^Set up\|^WARNING\|^\s*$" \
  | head -15 || true

echo ""
echo "=== Post-Deploy Complete ==="
echo ""
