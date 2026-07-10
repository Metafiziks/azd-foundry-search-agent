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

# Invoke with retry — the container needs time to cold-start after deploy.
# A new session triggers startup, which logs the hosting identity OID.
echo "► Invoking agent to capture hosting identity (may take ~60s on cold start)..."
HOSTING_OID=""
for attempt in 1 2 3; do
  echo "  Attempt ${attempt}/3..."
  azd ai agent invoke search-agent \
    '{"messages":[{"role":"user","content":"startup"}]}' \
    > /dev/null 2>&1 && break || true
  sleep 15
done

# Give log infrastructure a moment to flush
sleep 5

# Extract OID using a proper UUID pattern (more reliable than sed)
LOGS=$(azd ai agent monitor search-agent --tail 100 2>&1 || true)
HOSTING_OID=$(echo "$LOGS" | grep "IDENTITY OID" \
  | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
  | head -1 || true)

if [ -z "$HOSTING_OID" ]; then
  echo ""
  echo "⚠  Could not auto-detect hosting identity OID."
  echo "   Run the agent manually once, then grant the role:"
  echo ""
  echo "     azd ai agent invoke search-agent '{\"messages\":[{\"role\":\"user\",\"content\":\"test\"}]}'"
  echo "     OID=\$(azd ai agent monitor search-agent --tail 30 2>&1 | grep 'IDENTITY OID' | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)"
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

# RBAC can take up to 5 minutes to propagate globally.
echo ""
echo "  Waiting 2 minutes for RBAC propagation..."
sleep 120

echo "► Verifying agent search..."
azd ai agent invoke search-agent \
  '{"messages":[{"role":"user","content":"What documents are available?"}]}' \
  2>&1 | grep -v "^Session\|^Conversation\|^Trace\|^Next\|^azd\|^Set up\|^WARNING\|^\s*$" \
  | head -15 || true

echo ""
echo "=== Post-Deploy Complete ==="
echo ""
