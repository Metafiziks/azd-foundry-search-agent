# azd-foundry-search-agent

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/Metafiziks/azd-foundry-search-agent)

An [Azure Developer CLI (azd)](https://aka.ms/azd) template that deploys an **Azure AI Foundry hosted agent** backed by your own document corpus. Ask questions in natural language — the agent synthesizes answers from your documents and cites the source files with direct links.

```
Agent: What should a floor supervisor do when equipment fails?

[search-agent] Here's a practical sequence broken down by safety, quality,
and maintenance...

Sources:
- [Lockout Tagout Procedure](https://....blob.core.windows.net/docs/safety/lockout_tagout_procedure.txt)
- [Hydraulic Press Troubleshooting Guide](https://....blob.core.windows.net/docs/maintenance/hydraulic_press_troubleshooting.txt)
- [Finished Goods Defect Classification Standard](https://....blob.core.windows.net/docs/quality/finished_goods_defect_standard.txt)
```

## What it deploys

| Resource | Purpose |
|---|---|
| Azure AI Foundry | Hosts the agent and gpt-5 model deployment |
| Azure AI Search | Indexes documents and serves semantic search |
| Azure Blob Storage | Stores the document corpus (public read for citation links) |

## Architecture

```
User question
     │
     ▼
Foundry Hosted Agent (Python, remote_build)
     │  calls search_knowledge_base()
     ▼
Azure AI Search  ◄── indexes every 2 hours
     │
     ▼
Azure Blob Storage  (docs/ container, public read)
```

The agent runs entirely in Foundry's hosting infrastructure — no container registry or ACA environment to manage. Its managed identity is granted `Search Index Data Reader` automatically at deploy time.

## Prerequisites

- [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) with the `azure.ai.agents` extension (`>= 1.0.0-beta.4`)
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
- Azure subscription with **gpt-5 quota** in `eastus2` (or adjust `AZURE_LOCATION` and the model in postprovision)

Install the azd extension if you haven't already:

```bash
azd extension add azure.ai.agents
```

## Quick start

```bash
git clone https://github.com/Metafiziks/azd-foundry-search-agent
cd azd-foundry-search-agent

azd auth login
az login

azd up
```

`azd up` runs the full pipeline (~9 minutes on a fresh environment):

| Step | What happens |
|---|---|
| Provision | Creates AI Foundry account + project, AI Search, Blob Storage |
| Post-provision | Deploys gpt-5, uploads `docs/` to blob, creates search index + indexer, waits for Foundry data plane |
| Deploy | Packages and deploys the hosted agent via `remote_build` |
| Post-deploy | Discovers agent hosting identity, grants `Search Index Data Reader` |

## Automated evaluations

The template includes a built-in eval suite that runs automatically after each `azd up`:

| Metric | Threshold | How it's measured |
|---|---|---|
| Faithfulness | ≥ 0.70 | LLM judge: claims grounded in citations |
| Answer Relevance | ≥ 0.75 | LLM judge: answer addresses the question |
| Citation Recall | ≥ 0.60 | Expected source doc appeared in citations |
| Keyword Recall | ≥ 0.65 | Key phrases from expected answer found in response |
| p95 Latency | ≤ 10,000ms | Wall-clock time for agent response |

```bash
# Run evals standalone (agent must already be deployed)
bash scripts/eval.sh

# Deterministic metrics only (no LLM judge, faster)
bash scripts/eval.sh --no-judge
```

**Keeping evals in sync with your docs:**

When you update `docs/`, regenerate the eval cases before re-running:

```bash
eval $(azd env get-values)
python3 scripts/generate_eval_cases.py   # reads docs/, writes tests/eval_cases.json
bash scripts/eval.sh
```

`azd up` does this automatically — the post-deploy script regenerates cases after every doc change.

## CI/CD (GitHub Actions)

Copy the workflow files to activate them:

```bash
mkdir -p .github/workflows
cp workflows/*.yml .github/workflows/
git add .github/workflows && git commit -m "Activate CI workflows" && git push
```

Required secrets (`Settings → Secrets and variables → Actions`):

| Secret | Value |
|---|---|
| `AZURE_CLIENT_ID` | App registration client ID (federated credentials) |
| `AZURE_TENANT_ID` | Azure tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID |

Required variables:

| Variable | Value |
|---|---|
| `AZURE_ENV_NAME` | Your `azd` environment name |
| `AZURE_LOCATION` | Azure region (default: `eastus2`) |

**Workflow behavior:**

- `deploy.yml` — triggered on push; re-uploads docs and re-indexes if `docs/` changed; redeploys agent if `src/` or `scripts/` changed; runs evals after deploy
- `run-evals.yml` — triggered weekly (Monday 6am UTC) and on-demand; runs evals against the deployed agent

## Bring your own documents

1. Replace the files in `docs/` with your own content. Subdirectory structure is preserved as metadata (e.g., `docs/safety/`, `docs/hr/`, `docs/legal/`).
2. Supported formats: `.txt`, `.pdf`, `.docx`, `.md`
3. Re-run `azd up` to re-index and redeploy.

Optionally, update the agent system prompt in `src/agent/main.py` to fit your domain:

```python
INSTRUCTIONS = """
You are a knowledgeable assistant that answers questions based on
[your org]'s documents and procedures.
...
"""
```

## Test the agent

```bash
# Basic invocation
azd ai agent invoke search-agent \
  '{"messages":[{"role":"user","content":"What documents are available?"}]}'

# Cross-document synthesis
azd ai agent invoke search-agent \
  '{"messages":[{"role":"user","content":"Summarize the key safety requirements across all procedures."}]}'
```

Or open the agent playground link printed at the end of `azd up`.

## Environment variables

These are set automatically by `azd up` and available as azd env values:

| Variable | Description |
|---|---|
| `AZURE_ENV_NAME` | Environment name — drives all resource names |
| `AZURE_LOCATION` | Primary Azure region (default: `eastus2`) |
| `AZURE_SUBSCRIPTION_ID` | Set automatically from `az login` |
| `AZURE_SEARCH_ENDPOINT` | AI Search service URL |
| `AZURE_SEARCH_INDEX` | Search index name |
| `FOUNDRY_PROJECT_ENDPOINT` | Foundry data plane project URL |
| `AGENT_SEARCH_AGENT_ENDPOINT` | Deployed agent version endpoint |

Override region before provisioning:

```bash
azd env set AZURE_LOCATION eastus
azd up
```

## Redeployment

After changing documents or agent code:

```bash
azd up          # full reprovision + redeploy
# or
azd deploy      # agent code changes only (skip infra)
```

## Cleanup

```bash
azd down --purge
```

> **Note:** `--purge` permanently deletes the Cognitive Services account. Without it, the account is soft-deleted and the same environment name cannot be reused for 48 hours.

## Comparison across cloud providers

| | This template (Azure) | [AWS](https://github.com/Metafiziks/aws-bedrock-agent) | [GCP](https://github.com/Metafiziks/gcp-search-agent) |
|---|---|---|---|
| Provision | `azd provision` | `bash scripts/provision.sh` | `bash scripts/provision.sh` |
| LLM | GPT-5 (Azure AI Foundry) | Amazon Nova Lite (Bedrock) | Gemini 2.5 Flash (Vertex AI) |
| Agent SDK | AI Foundry hosted agent | Bedrock Agents (managed) | Google ADK + Cloud Run |
| RAG | Azure AI Search | Bedrock Knowledge Bases | Vertex AI Search Enterprise |
| Vector store | Azure AI Search (built-in) | OpenSearch Serverless | Vertex AI Search (built-in) |
| Auth | Azure OIDC | GitHub OIDC | Workload Identity Federation |
| Eval judge | GPT-5 | Amazon Nova Pro | Gemini 2.5 Flash |
| Teardown | `azd down` | `bash scripts/teardown.sh` | `bash scripts/teardown.sh` |

## Troubleshooting

**`azd deploy` fails with `Project not found`**
The Foundry data plane takes a few minutes to initialize after provisioning. Re-run `azd deploy` — the post-provision script waits for readiness, but `azd deploy` run in isolation does not.

**AI Search capacity error**
`eastus2` may be capacity-constrained. The post-provision script automatically tries `eastus`, `westus2`, and other regions in sequence.

**`ServiceDeleting` error on Search**
Occurs when re-running `azd up` soon after `azd down`. The script retries with exponential backoff — wait a few minutes and re-run.

**Agent returns stale responses in CLI**
The CLI reuses conversation history across invokes. Use the portal playground link (printed after `azd up`) for a fresh session each time.
