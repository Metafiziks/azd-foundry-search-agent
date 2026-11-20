# azd-foundry-search-agent

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/Metafiziks/azd-foundry-search-agent)

An [Azure Developer CLI (azd)](https://aka.ms/azd) template that deploys an **Azure AI Foundry hosted agent** backed by your own document corpus. Ask questions in natural language — the agent synthesizes answers from your documents and cites the source files with direct links. Includes a built-in **automated evaluation suite** that scores every deployment on faithfulness, answer relevance, citation accuracy, latency, and memory recall using GPT-5 as an LLM judge, plus an **ML observability layer** with Azure AI Search semantic ranking, Foundry Memory telemetry, IsolationForest anomaly detection, and HHEM hallucination scoring backed by Log Analytics KQL for alerting.

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
| Azure AI Foundry Memory Store (preview) | Stores scoped user/session memories for continuity |
| Azure AI Search | Indexes documents and serves semantic search |
| Azure Blob Storage | Stores the document corpus (public read for citation links) |

## Architecture

```
User question
     │
     ▼
Foundry Hosted Agent (Python, remote_build)
     │
     ├── Foundry Memory Store (preview)
     │       user/session preferences and workflow continuity
     │
     │  calls search_knowledge_base()
     ▼
Azure AI Search  ◄── indexes every 2 hours
     │
     ▼
Azure Blob Storage  (docs/ container, public read)
```

The agent runs entirely in Foundry's hosting infrastructure — no container registry or ACA environment to manage. Its managed identity is granted `Search Index Data Reader` for AI Search and Foundry Memory RBAC (`Foundry User` and `Cognitive Services OpenAI User`) automatically at deploy time.

Memory is intentionally separate from document retrieval. Azure AI Search remains the source of truth for procedures and citations; Foundry Memory only supplies scoped continuity such as stable user preferences, session context, or workflow hints.

## Prerequisites

- [Current Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) with the `microsoft.foundry` extension (includes the Foundry Projects and Agents providers)
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
- Azure subscription with **gpt-5 GlobalStandard quota** in `eastus2`, or set `AZURE_AI_MODEL_NAME`, `AZURE_AI_MODEL_VERSION`, and `AZURE_AI_MODEL_CAPACITY` with `azd env set` to use another supported GlobalStandard model with quota. For example, `gpt-5-mini` version `2025-08-07` can be used to smoke-test protected retrieval.

Install the azd extension if you haven't already:

```bash
azd extension install microsoft.foundry
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
| Memory Recall | case-level | Two-turn recall case: first turn stores a preference, second turn asks for it |

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

The generator always appends `memory-preference-recall`, a two-turn memory case. If `MEMORY_ENABLED=true` and a `MEMORY_STORE_NAME` exists, evals exercise managed memory with scoped `x-agent-user-id` / `x-memory-user-id` headers. If memory preview provisioning is unavailable and `MEMORY_OPTIONAL=true`, the memory case is marked skipped rather than failing unrelated RAG evals.

For quota-constrained Foundry deployments, the eval runner supports `EVAL_AGENT_TIMEOUT_SECONDS`, `EVAL_INTER_CASE_WAIT_SECONDS`, `EVAL_WARMUP_ENABLED`, and `EVAL_WARMUP_QUESTION`. `postdeploy.sh` also honors `POSTDEPLOY_EVAL_ARGS` and `BASELINE_RUNS`; set `BASELINE_RUNS=0` only when you intentionally want to skip extra telemetry-only baseline runs for IsolationForest training.

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

The default `sample` mode uses publicly readable Blob citation URLs. Use it only
for public documents. For an internal knowledge base, use the protected mode below.

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

## Protect documents with Microsoft Purview

Set `KB_ACCESS_MODE=purview` to use **existing Microsoft Purview sensitivity
labels** with [Foundry IQ][foundry-iq] (Azure AI Search, Purview integration in public
preview). The Blob indexer extracts text and labels into a protected index.
A Foundry IQ knowledge base retrieves from that index under the signed-in
user's permissions. The agent uses only individually labeled references to
ground answers; it never falls back to the sample index or uses the model to
make access decisions.

1. Use a fresh azd environment in the Purview tenant. Sign in to `az` and
   `azd` with the same deployment identity, which needs permission to create
   resources and assign Azure RBAC roles in the environment. If you have
   subscriptions in multiple tenants, sign in explicitly to the target tenant.

   ```bash
   az login --tenant <tenant-id>
   azd auth login --tenant-id <tenant-id>
   azd env new <unique-environment> --subscription <subscription-id> --location eastus2
   azd env set KB_ACCESS_MODE purview
   azd up --no-prompt
   ```

   For an interactive deployment with the acceptance gate below, use
   `azd provision` instead of `azd up` for this first step.

   The hooks create dedicated Search and Storage resources in the environment's
   resource group. Search uses its system-assigned identity and RBAC, not API
   keys. The `docs` Blob container has no anonymous or shared-key access; Blob
   soft delete is enabled. The hook configures a Purview-enabled index and
   Blob indexer, an index-backed knowledge source, and a model-free, extractive
   Foundry IQ knowledge base. It never uploads the public sample corpus.
   Post-deploy assigns [Reader and Search Index Data Reader][search-roles]
   on the dedicated protected Search service for knowledge-base retrieval and
   schema checks, not elevated read. Don't add unrelated indexes to this service.
2. Have a tenant administrator approve and assign the
   [Purview extraction roles][purview-index] to the **Search identity** printed
   by the provision hook, not to the agent. `azd` cannot grant those tenant
   permissions. If Purview rejects index provisioning before the grant, rerun
   `azd up` in the same environment after approval.
3. Apply Purview labels to supported files **before** ingestion. Use a label
   whose rights grant extraction only to intended users or groups; a label
   that only classifies a document does not restrict its audience. Use a local
   corpus directory **outside** this repo. Give the ingest operator `Storage
   Blob Data Contributor` on the `docs` container, `Reader` on the Storage
   account, and `Search Service Contributor` on the dedicated Search service.
   The deployer already has the Search role. Run:

   ```bash
   python3 scripts/ingest_purview.py \
     --subscription <subscription-id> --search-service <protected-search-name> \
     --index <PURVIEW_SEARCH_INDEX> --storage-account <PURVIEW_STORAGE_ACCOUNT> \
     --directory /path/to/approved-corpus
   ```

   The script checks the destination, uploads with Entra authentication, and
   waits for a new indexer result with processed documents and no errors or
   warnings. It does not apply labels or establish that a user has access. An
   empty successful run from initial provisioning is not ingestion. If a
   run fails, inspect the indexer status before tearing down the environment;
   use `--show-indexer-error` for the run-level diagnostic in a trusted terminal.

**Connect the existing company app.** Its trusted backend exchanges the
user's validated Entra token [on behalf of the user][foundry-obo] for a token
with scope `https://search.azure.com/.default`. The backend calls the Foundry
Responses endpoint with these headers:

| Header | Value |
|---|---|
| `Authorization` | `Bearer <backend Foundry workload token>` |
| `x-ms-user-identity` | Object ID of the signed-in user from the backend's validated identity |
| `x-client-search-access-token` | Raw delegated Search access token for that same user, without `Bearer` |

Use Foundry Agent Consumer and the `UserIdentityImpersonation/action`
permission for the backend workload. Limit invocation of the protected
agent to trusted backend identities. Don't accept user IDs or Search tokens
from browser-supplied headers, and don't log or trace them. Send
`{"input":"<question>","store":false,"background":false}` with no conversation
or previous response. The backend should check the HTTP and Responses
statuses, and must not cache protected results or replay previous answers into
later requests. The agent returns source filenames, not Blob URLs.

**Verify during deployment.** In a fresh Purview test environment, use a
dedicated local directory containing only two synthetic fixtures: a restricted
Office document and an unlabeled control with a different fact. Save this
non-secret JSON outside the checkout; do not put access tokens in it:

```json
{
  "directory": "/absolute/path/to/synthetic-fixtures",
  "allowed_oid": "<approved-user-object-id>",
  "denied_oid": "<denied-user-object-id>",
  "labeled_blob": "approved.docx",
  "unlabeled_blob": "control.docx",
  "question": "What is the restricted test access code?",
  "expected": "ORCHID-47",
  "unlabeled_question": "What is the unlabeled control code?",
  "unlabeled_expected": "MAPLE-82"
}
```

Run initial provisioning so the tenant admin can approve the **Search**
managed identity. Grant the CLI ingest operator the Blob and Search roles
listed above. From an interactive terminal with `httpx` installed for
`python3` (for example, in an active virtual environment), set the gate and
deploy:

```bash
azd env set PURVIEW_ACCEPTANCE_CONFIG /absolute/path/to/purview-acceptance.json
azd up
```

The post-deploy hook uploads the two files, requires a fresh successful indexer
run, then prompts without echo for the trusted backend Foundry token and the
two users' delegated Search tokens. It fails the deployment command unless the
agent cites the restricted file and returns the fact for the approved user,
returns a completed no-document response for the denied user, and does not
reveal or cite the unlabeled control. It also checks missing delegation,
stateful requests, and anonymous Blob access. The gate verifies **retrieval
permissions**, not whether the downloaded file itself is encrypted at rest.
It does not automatically roll back; tear down a failed test environment
with the command below. With no config, protected `azd up` provisions only;
sample-mode behavior is unchanged.

**Run acceptance separately.** To check a deployed agent after manual corpus
ingestion, including the expired-token case, run from a terminal:

```bash
python3 scripts/test_purview_live.py \
  --account <foundry-account> --project <foundry-project> \
  --storage-account <PURVIEW_STORAGE_ACCOUNT> \
  --allowed-oid <approved-user-object-id> --denied-oid <denied-user-object-id> \
  --question 'What is the synthetic access-test code?' --expected '<test-code>' \
  --labeled-blob approved.docx --unlabeled-blob control.docx \
  --unlabeled-question 'What is the synthetic control code?'
```

Provide the test backend's Foundry token and the users' delegated Search
tokens at the hidden prompts, including an expired token for the negative
case. The check requires a cited answer for the approved user, no answer
for the denied user or unlabeled control, rejection of missing or expired
delegation and stateful requests, and denial of anonymous reads. Source
relabeling and soft deletes only take effect in Search after a successful
indexer run (every two hours by default).
For an urgent removal, take the affected index or endpoint out of service until
the updated results are verified. See [query-time limitations][purview-query],
including same-tenant users only.

Protected mode skips sample ingestion, Memory Store, and automated sample
evals. Do not configure content recording for protected requests or put
restricted files in `docs/`, even in protected mode. If using the optional
sample GitHub workflows, set repository variable `KB_ACCESS_MODE=purview` to
disable their deployment and scheduled-eval jobs for this environment.

After a test, run `azd down --force --purge --no-prompt` in that environment
and verify its resource group is gone. Check any separately created tenant
test users, labels, or app registrations with the tenant administrator; `azd
down` does not manage those directory objects.

[purview-index]: https://learn.microsoft.com/azure/search/search-indexer-sensitivity-labels
[purview-query]: https://learn.microsoft.com/azure/search/search-query-sensitivity-labels
[foundry-iq]: https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-retrieve
[search-roles]: https://learn.microsoft.com/azure/search/search-security-rbac
[foundry-obo]: https://learn.microsoft.com/azure/foundry/agents/how-to/use-on-behalf-of-flow

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

## Memory layer (preview)

This template uses Azure AI Foundry Agent Service Memory Store when the current SDK/API and subscription support the preview. `postprovision.sh` deploys `text-embedding-3-small`, creates or reuses `MEMORY_STORE_NAME` through `azure-ai-projects>=2.3.0`, and sets the hosted-agent environment. At runtime, a context provider reads relevant memories before each model call and queues memory updates after the response.

**Behavior and limits:**

- Memory is optional by default because the service is in public preview. Set `MEMORY_OPTIONAL=false` to make provisioning/runtime failures fail fast.
- Memory is scoped separately from RAG. Use it for user preferences and workflow continuity, not for authoritative procedure content.
- The default scope is `{{$userId}}` for Foundry-hosted identity scoping. For local or single-tenant testing, set `MEMORY_SCOPE=user:{memory_user_id}` and `MEMORY_USER_ID=<stable-id>`. To force per-session isolation, set `MEMORY_SCOPE=session:{session_id}`.
- Memory writes use the Memory Store update poller. With optional memory enabled, write completion is polled in a background task so response latency is not blocked; set `MEMORY_OPTIONAL=false` to await the poller and fail fast on write errors.
- Provisioning grants `Foundry User` and `Cognitive Services OpenAI User` on the Foundry account/project to the hosted agent and project identities so Memory Store can read/write and run embedding operations.
- Avoid storing sensitive personal data, secrets, credentials, financial data, or precise location data. The store is configured to favor stable manufacturing workflow preferences.

Memory-specific telemetry is written to `AgentTelemetry_CL` with `MemoryEnabled`, `MemoryReadCount`, `MemoryWriteCount`, `MemoryLatencyMs`, and `MemoryStatus` fields when Log Analytics ingestion is configured.

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
| `AZURE_AI_EMBEDDING_MODEL_DEPLOYMENT_NAME` | Embedding deployment used by Foundry Memory Store (default `text-embedding-3-small`) |
| `MEMORY_ENABLED` | Enables Foundry Memory in the hosted agent when `true` |
| `MEMORY_OPTIONAL` | Allows safe fallback without memory if preview APIs/quotas are unavailable (default `true`) |
| `MEMORY_STORE_NAME` | Foundry Memory Store name created by `postprovision.sh` |
| `MEMORY_SCOPE` | Scope template for memory isolation; default `{{$userId}}`, with `{session_id}` and `{memory_user_id}` supported by the local provider |
| `MEMORY_INCLUDE_STATIC_PROFILE` | Injects static profile memories on every request when `true`; default `false` keeps profile memories limited to memory/preference-style questions so RAG answers remain source-of-truth |
| `MEMORY_UPDATE_DELAY_SECONDS` | Debounce before memory writes are committed (default `5` for template/eval friendliness) |

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

**Memory eval is skipped**
Memory is in Foundry preview. Confirm `MEMORY_ENABLED=true`, `MEMORY_STORE_NAME` is set, and the embedding deployment exists. If provisioning logs show preview API, quota, or RBAC errors and `MEMORY_OPTIONAL=true`, the template continues with RAG-only behavior and marks memory evals skipped.

**Memory writes fail with 401/403**
Grant the deployed agent hosting identity `Cognitive Services OpenAI User` on the Foundry account/project scope. `postdeploy.sh` attempts this automatically after discovering the hosting identity.

**Generated evals timeout or hit transient 500/503**
Foundry hosted agents can be cold-started or quota constrained. Increase `EVAL_AGENT_TIMEOUT_SECONDS`, add `EVAL_INTER_CASE_WAIT_SECONDS`, keep warmup enabled, or run `POSTDEPLOY_EVAL_ARGS=--no-judge` when validating deterministic RAG/memory behavior separately from judge-model availability. The runner still fails real answer-quality issues instead of marking failed responses as passing.

---

## ML Observability

Beyond pass/fail eval scores, this template ships a production-ready ML observability layer that runs alongside the Foundry hosted agent and learns what "healthy" looks like for your specific document corpus.

### Architecture

```
Request
  │
  ▼
Foundry Hosted Agent
  │
  ├── Azure AI Search  ──→  Semantic ranking (QueryType.SEMANTIC)
  │   (built-in)              Extracts @search.score + @search.reranker_score
  │                           No extra API call — semantic tier included in Standard SKU
  │
  ├── IsolationForest  ──→  Anomaly score from 6-d feature vector
  │   (loaded from Blob)      [retrieval_mean, retrieval_std, retrieval_entropy,
  │                            chunk_count, reranker_mean, search_latency_ms]
  │
  └── Log Analytics Sink  ──→  AgentTelemetry_CL custom log table
                                (DCE ingest via azure-monitor-ingestion SDK)
                                → KQL queries + series_decompose_anomalies()
```

### ML Models

| Model | Type | Purpose | Location |
|-------|------|---------|----------|
| Azure AI Search Semantic Ranker | Cross-encoder reranker | Re-rank retrieved docs by semantic relevance | AI Search (Standard tier) |
| `IsolationForest` | Unsupervised anomaly detector | Flag unusual retrieval patterns | Blob Storage `models/iforest.pkl` |
| `vectara/hallucination_evaluation_model` (HHEM-2.1) | Hallucination classifier | Score answer faithfulness in evals | HuggingFace Hub |

### Auto-training

The `postdeploy.sh` script:
1. Runs the eval suite 5× with `EVAL_IS_BASELINE=true` → rows ingested to `AgentTelemetry_CL`
2. Queries those rows via `azure-monitor-query` KQL client
3. Trains IsolationForest and uploads `iforest.pkl` to Blob Storage `models/` container
4. Foundry container loads the model on next cold start

The GitHub Actions workflow (`.github/workflows/retrain-observability.yml`) retrains weekly on the last 30 days of Log Analytics data.

### Log Analytics Schema (AgentTelemetry_CL)

| Column | Type | Description |
|--------|------|-------------|
| `TimeGenerated` | datetime | Ingestion timestamp |
| `RequestId_s` | string | Unique request UUID |
| `RetrievalScoreMean_d` | real | Mean Azure AI Search relevance score |
| `RerankerScoreMean_d` | real | Mean semantic reranker score |
| `ChunkCount_d` | real | Number of chunks retrieved |
| `SearchLatencyMs_d` | real | End-to-end retrieval latency (ms) |
| `HhemScore_d` | real | HHEM hallucination probability (eval runs only) |
| `AnomalyScore_d` | real | IForest score (negative = more anomalous) |
| `IsAnomaly_b` | boolean | True if anomaly detected |
| `IsBaseline_b` | boolean | True for bootstrap training rows |
| `MemoryEnabled_b` | boolean | Whether Foundry Memory was enabled for the row |
| `MemoryReadCount_d` | real | Number of memory records retrieved/injected |
| `MemoryWriteCount_d` | real | Number of messages queued for memory extraction |
| `MemoryLatencyMs_d` | real | Memory read/write operation latency |
| `MemoryStatus_s` | string | Memory operation status (`read_ok`, `write_queued`, `read_failed`, etc.) |

### KQL Anomaly Queries

```kql
// Rolling anomaly rate — last 7 days
AgentTelemetry_CL
| where TimeGenerated > ago(7d)
| summarize total=count(), anomalies=countif(IsAnomaly_b == true) by bin(TimeGenerated, 1h)
| extend anomaly_rate = todouble(anomalies) / total
| render timechart

// Azure native time-series anomaly detection on retrieval scores
AgentTelemetry_CL
| where TimeGenerated > ago(30d)
| make-series avg_retrieval_score=avg(RetrievalScoreMean_d) on TimeGenerated step 1h
| extend anomalies=series_decompose_anomalies(avg_retrieval_score)
| render anomalychart

// HHEM trend — are answers becoming less faithful?
AgentTelemetry_CL
| where TimeGenerated > ago(30d)
| summarize avg_hhem=avg(HhemScore_d) by bin(TimeGenerated, 1d)
| render timechart
```

### Weekly Retrain

The `.github/workflows/retrain-observability.yml` workflow triggers every Sunday 02:00 UTC:
- Queries last 30 days from Log Analytics via `azure-monitor-query` SDK
- Retrains IsolationForest on production distribution
- Uploads new `iforest.pkl` to Blob Storage → agent picks up on next cold start

Required GitHub secrets/variables:
- `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` (OIDC)
- `LOG_ANALYTICS_WORKSPACE_ID` (set automatically by `postprovision.sh`)
- `BLOB_ACCOUNT_URL` (set automatically by `postprovision.sh`)

To trigger manually:
```bash
gh workflow run retrain-observability.yml
```
