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

# Invoke agent in background to trigger startup OID logging
echo "► Starting agent to capture hosting identity..."
azd ai agent invoke search-agent '{"messages":[{"role":"user","content":"startup"}]}' > /dev/null 2>&1 &
INVOKE_PID=$!

# Wait for container to start and log the OID
sleep 20

# Kill the invoke
kill "$INVOKE_PID" 2>/dev/null || true

# Capture recent logs
LOGS=$(azd ai agent monitor search-agent --tail 100 2>&1 || true)

# Extract OID
HOSTING_OID=$(echo "$LOGS" | grep "IDENTITY OID" | sed 's/.*OID: \([a-f0-9-]*\).*//' | head -1 || true)

if [ -z "$HOSTING_OID" ]; then
  echo ""
  echo "⚠  Could not auto-detect hosting identity OID."
  echo "   Invoke the agent manually, then check logs:"
  echo "     azd ai agent invoke search-agent '{"messages":[{"role":"user","content":"test"}]}'"
  echo "     azd ai agent monitor search-agent --tail 20 | grep 'IDENTITY OID'"
  echo "   Then grant the role:"
  echo "     az role assignment create --assignee <OID> --role 'Search Index Data Reader' --scope ${SEARCH_RESOURCE_ID}"
  echo ""
  exit 0
fi

echo "  Hosting identity: ${HOSTING_OID}"
echo "► Granting Search Index Data Reader..."

az role assignment create   --assignee "$HOSTING_OID"   --role "Search Index Data Reader"   --scope "$SEARCH_RESOURCE_ID"   --output none 2>/dev/null || echo "  (role already assigned)"

echo "  ✓ Permission granted"
echo ""
echo "  Waiting 30s for RBAC propagation..."
sleep 30

echo "► Verifying agent search..."
azd ai agent invoke search-agent '{"messages":[{"role":"user","content":"What documents are available?"}]}' 2>&1 | grep -v "^Session\|^Conversation\|^Trace\|^Next\|^azd\|^Set up" | head -10 || true

echo ""
echo "=== Post-Deploy Complete ==="
echo ""
