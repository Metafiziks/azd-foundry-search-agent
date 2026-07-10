# azd-foundry-search-agent

> **ℹ️ Deployment note:** `azd up` fully automates provision + deploy in a single step. The post-provision script waits for the Foundry data plane to be ready before deployment starts — no manual retries needed on a fresh environment.


An [Azure Developer CLI (azd)](https://aka.ms/azd) template that deploys an **Azure AI Foundry hosted agent** with knowledge-base search over your own document corpus.

## What it deploys

| Resource | Purpose |
|---|---|
| Azure AI Foundry | Hosts the agent with gpt-5 |
| Azure AI Search | Indexes and retrieves documents |
| Azure Blob Storage | Stores the document corpus |

## Prerequisites

- [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) `>= 1.0.0` with the `azure.ai.agents` extension
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (for postprovision scripts)
- Azure subscription with gpt-5 quota in `eastus2`

## Quick start

```bash
git clone https://github.com/Metafiziks/azd-foundry-search-agent
cd azd-foundry-search-agent

azd auth login
az login

azd up
```

`azd up` runs the full flow:
1. **Provision** — Creates AI Foundry, AI Search, and Blob Storage
2. **Postprovision** — Deploys a gpt-5 model, uploads `docs/` to blob, creates and runs the search indexer
3. **Deploy** — Builds and deploys the hosted agent
4. **Postdeploy** — Discovers the agent's hosting identity and grants it `Search Index Data Reader`

## Customizing your corpus

Replace the files in `docs/` with your own `.txt`, `.pdf`, or `.docx` files. Subdirectory structure is preserved as document metadata. Then redeploy:

```bash
azd up
```

Update the agent instructions in `src/agent/main.py` to match your domain.

## Testing

```bash
azd ai agent invoke search-agent '{"messages":[{"role":"user","content":"Your question here"}]}'
```

## Cleanup

```bash
azd down
```

## Architecture

```
User → azd ai agent invoke
         ↓
   Foundry Hosted Agent (search-agent)
         ↓ search_knowledge_base()
   Azure AI Search (RBAC, MSI auth)
         ↓
   Blob Storage (document corpus)
```

The agent runs in Foundry's hosting infrastructure. Its managed identity is automatically discovered at deploy time and granted `Search Index Data Reader`.
