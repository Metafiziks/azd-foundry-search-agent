# Protect documents with Microsoft Purview

Set `KB_ACCESS_MODE=purview` to use **existing Microsoft Purview sensitivity
labels** with [Foundry IQ][foundry-iq] (Azure AI Search, Purview integration in public
preview). The Blob indexer extracts text and labels into a protected index.
The agent requires a valid label on each returned reference, using the
annotated index field when the preview API omits reference-level label metadata.
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
