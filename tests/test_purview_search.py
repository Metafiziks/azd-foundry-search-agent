import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError

import httpx

from ingest_purview import corpus_files, index_uploaded_files, matches_corpus
from purview_graph import graph_principals
from purview_host import NO_DOCUMENTS, build_protected_host
from purview_search import ProtectedSearchError, PurviewSearch, TOKEN_HEADER
from setup_purview_index import definitions, deploy_search
from test_purview_live import acceptance_files, run_tests

LABEL = "12345678-4321-4321-4321-123456789abc"
DEFINITIONS = {
    f"/{collection}/{body['name']}": body
    for collection, body in definitions("protected", "/storage-id", "private-docs")
}
SCHEMA = DEFINITIONS["/indexes/protected"]


class ProtectedAcceptanceTests(unittest.TestCase):
    def test_graph_lookup_rejects_a_token_from_the_wrong_tenant(self):
        with (
            patch("purview_graph.subprocess.check_output", return_value=b'{"tenant":"wrong-tenant"}') as cli,
            patch("purview_graph.urlopen") as graph,
        ):
            with self.assertRaisesRegex(ValueError, "does not belong to the deployment tenant"):
                graph_principals("selected-subscription", "selected-tenant", None, None)
            self.assertIn("selected-subscription", cli.call_args.args[0])
            graph.assert_not_called()

    def test_postdeploy_propagates_acceptance_gate_failure_only_when_configured(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as temporary:
            path = Path(temporary)
            for name, script in {
                "az": """#!/usr/bin/env bash
case "$1 $2" in
    "cognitiveservices account") echo test-account ;;
    "account show") echo deployment-tenant ;;
    "role assignment") exit 0 ;;
    *) exit 98 ;;
esac
""",
                "python3": """#!/usr/bin/env bash
if [[ "$1" == */purview_graph.py ]]; then
    echo 12345678-4321-4321-4321-123456789abc
else
    printf 'gate command: %s\\n' "$*"
    exit 7
fi
""",
            }.items():
                command = path / name
                command.write_text(script)
                command.chmod(0o700)
            env = {
                "PATH": f"{path}{os.pathsep}{os.environ['PATH']}",
                "AZURE_RESOURCE_GROUP": "rg-test",
                "AZURE_ENV_NAME": "test-project",
                "KB_ACCESS_MODE": "purview",
                "AZURE_SUBSCRIPTION_ID": "test-subscription",
                "PURVIEW_SEARCH_INDEX": "test-index",
                "PURVIEW_STORAGE_ACCOUNT": "teststorage",
                "AZURE_SEARCH_RESOURCE_ID": "/searchServices/test-search",
            }
            for config in ("", str(path / "fixtures.json")):
                with self.subTest(configured=bool(config)):
                    result = subprocess.run(
                        ["bash", str(root / "scripts/postdeploy.sh")],
                        env=env | {"PURVIEW_ACCEPTANCE_CONFIG": config},
                        capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(result.returncode, 7 if config else 0, result.stderr)
                    if config:
                        self.assertIn(f"--config {config} --account test-account --project test-project", result.stdout)
                        self.assertIn("--search-service test-search --index test-index", result.stdout)
                    else:
                        self.assertNotIn("gate command:", result.stdout)

    def test_gate_requires_exactly_two_named_fixtures(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = SimpleNamespace(directory=root, labeled_blob="approved.docx", unlabeled_blob="control.docx")
            for name in (args.labeled_blob, args.unlabeled_blob):
                (root / name).touch()
            self.assertEqual(len(acceptance_files(args)[1]), 2)
            (root / "unrelated.pdf").touch()
            with self.assertRaisesRegex(ValueError, "exactly the two named"):
                acceptance_files(args)

    def test_gate_requires_a_real_denial_and_rejects_control_disclosure(self):
        args = SimpleNamespace(
            account="test-account", project="test-project", storage_account="teststorage",
            allowed_oid="allowed", denied_oid="denied", expected="ORCHID-47", labeled_blob="approved.docx",
            question="What is the restricted code?", unlabeled_question="What is the control code?",
            unlabeled_blob="control.docx", unlabeled_expected="MAPLE-82",
        )

        def result(status, text=None):
            output = [] if text is None else [{"type": "message", "content": [{"type": "output_text", "text": text}]}]
            return httpx.Response(status, json={"status": "completed", "output": output})

        for denied, unlabeled, error in (
            (result(200, NO_DOCUMENTS), result(200, "Not in approved.docx."), None),
            (result(401), result(200, NO_DOCUMENTS), "denied: expected a completed response"),
            (result(200, NO_DOCUMENTS), result(200, "MAPLE-82"), "disclosed the control fact"),
            (result(200, NO_DOCUMENTS), result(200, "See control.docx."), "cited the control file"),
        ):
            with self.subTest(error=error):
                client = Mock()
                client.post.side_effect = [
                    result(200, "ORCHID-47 (approved.docx)"), denied, unlabeled, result(401), result(400),
                ]
                client.head.return_value = httpx.Response(403)
                if error is not None:
                    with self.assertRaisesRegex(RuntimeError, error):
                        run_tests(client, args, "backend-token", "allowed-token", "denied-token")
                else:
                    run_tests(client, args, "backend-token", "allowed-token", "denied-token")
                    self.assertEqual(client.post.call_count, 5)
                allowed_request, denied_request = client.post.call_args_list[:2]
                self.assertEqual(allowed_request.kwargs["headers"][TOKEN_HEADER], "allowed-token")
                self.assertEqual(denied_request.kwargs["headers"][TOKEN_HEADER], "denied-token")
                self.assertEqual(allowed_request.kwargs["json"], denied_request.kwargs["json"])

        client = Mock()
        client.post.side_effect = [
            httpx.ReadTimeout("cold start"),
            result(200, "ORCHID-47 (approved.docx)"),
            result(200, NO_DOCUMENTS), result(200, NO_DOCUMENTS), result(401),
            httpx.ReadTimeout("stateful requests must not be replayed"),
        ]
        with self.assertRaises(httpx.ReadTimeout):
            run_tests(client, args, "backend-token", "allowed-token", "denied-token")
        self.assertEqual(client.post.call_count, 6)
        self.assertEqual(client.post.call_args_list[0], client.post.call_args_list[1])


class ProtectedProvisioningTests(unittest.TestCase):
    def test_ingestion_verifies_blob_destination_even_when_search_redacts_credentials(self):
        config = dict(definitions("protected", "/storage-id", "docs"))
        source = config["datasources"] | {"credentials": {"connectionString": None}}
        account = {"id": "/storage-id", "allowBlobPublicAccess": False, "allowSharedKeyAccess": False}
        self.assertTrue(matches_corpus(source, config["indexers"], SCHEMA, account, "protected"))
        self.assertFalse(matches_corpus(source, config["indexers"], SCHEMA, account | {"id": "/other"}, "protected"))

    def test_ingestion_rejects_empty_corpus_and_symlinks(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "notes.md").touch()
            with self.assertRaisesRegex(ValueError, "No supported documents"):
                corpus_files(root)
            (root / "alias.docx").symlink_to(root / "notes.md")
            with self.assertRaisesRegex(ValueError, "symlinks"):
                corpus_files(root)

    def test_indexer_waits_for_fresh_run_and_rejects_zero_processed(self):
        def result(start, items):
            return json.dumps({"lastResult": {
                "startTime": start, "endTime": start, "status": "success",
                "itemsProcessed": items, "itemsFailed": 0, "warnings": [], "errors": [],
            }}).encode()

        old = result("old", 0)
        for processed in (0, 1):
            with (
                patch("ingest_purview.request", side_effect=[old, b"", old, result("new", processed)]) as request,
                patch("ingest_purview.time.sleep") as sleep,
            ):
                if processed:
                    self.assertEqual(index_uploaded_files("https://example.test", "index", "token", 1, 10), 1)
                else:
                    with self.assertRaisesRegex(RuntimeError, "processed=0"):
                        index_uploaded_files("https://example.test", "index", "token", 1, 10)
                self.assertEqual([call.args[0] for call in request.call_args_list], ["GET", "POST", "GET", "GET"])
                sleep.assert_called_once()

    def test_explicit_model_is_deployed_even_if_other_models_exist(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as temporary:
            path = Path(temporary)
            for name, script in {
                "az": """#!/usr/bin/env bash
case "$*" in
    "cognitiveservices account deployment list "*)
        printf '%s\\n' '[{"name":"unrelated-model","properties":{"model":{"name":"unrelated-model"}}}]'
        ;;
    "cognitiveservices account deployment create "*)
        printf '%s\\n' "$*"
        ;;
    *) exit 98 ;;
esac
""",
                "azd": """#!/usr/bin/env bash
printf '%s\\n' "$*"
exit 99
""",
            }.items():
                command = path / name
                command.write_text(script)
                command.chmod(0o700)
            result = subprocess.run(
                ["bash", str(root / "scripts/postprovision.sh")],
                env={
                    "PATH": f"{path}{os.pathsep}{os.environ['PATH']}",
                    "AZURE_RESOURCE_GROUP": "rg-test",
                    "AZURE_AI_ACCOUNT_NAME": "test-account",
                    "AZURE_ENV_NAME": "test",
                    "KB_ACCESS_MODE": "purview",
                    "AZURE_SUBSCRIPTION_ID": "test-subscription",
                    "AZURE_AI_MODEL_NAME": "gpt-5-mini",
                    "AZURE_AI_MODEL_VERSION": "2025-08-07",
                    "AZURE_AI_MODEL_CAPACITY": "3",
                },
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(result.returncode, 99, result.stderr)
        self.assertIn("--deployment-name gpt-5-mini --model-name gpt-5-mini --model-version 2025-08-07", result.stdout)
        self.assertIn("--sku-capacity 3", result.stdout)
        self.assertIn("env set AZURE_AI_MODEL_DEPLOYMENT_NAME gpt-5-mini", result.stdout)

    def test_managed_setup_retries_permission_propagation_and_resumes(self):
        forbidden = HTTPError("https://example.test", 403, "Forbidden", {}, None)
        with (
            patch("setup_purview_index.request", side_effect=[forbidden, b""]) as request,
            patch("setup_purview_index.time.sleep") as sleep,
        ):
            deploy_search("https://example.test", "app-token", SCHEMA, resume=True)
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(call.kwargs["create_only"] is False for call in request.call_args_list))
        sleep.assert_called_once()

    def test_standalone_setup_does_not_replace_an_existing_index(self):
        conflict = HTTPError("https://example.test", 412, "Already exists", {}, io.BytesIO(b"existing index"))
        with (
            patch("setup_purview_index.request", side_effect=conflict) as request,
            self.assertRaisesRegex(SystemExit, "HTTP 412: existing index"),
        ):
            deploy_search("https://example.test", "app-token", SCHEMA, resume=False)
        request.assert_called_once_with("PUT", "https://example.test", "app-token", SCHEMA, create_only=True)


class ProtectedRetrievalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.credential = SimpleNamespace(get_token=AsyncMock(return_value=SimpleNamespace(token="app-token")))

    def client(self, transport):
        return httpx.AsyncClient(transport=transport, base_url="https://test", trust_env=False)

    async def test_only_labeled_results_get_to_the_model_and_only_search_sees_the_token(self):
        def search_response(request):
            self.assertEqual(request.headers["authorization"], "Bearer app-token")
            if request.method == "GET":
                self.assertNotIn("x-ms-query-source-authorization", request.headers)
                return httpx.Response(200, json=DEFINITIONS[request.url.path])
            self.assertEqual(request.url.path, "/knowledgebases/protected-kb/retrieve")
            self.assertEqual(request.headers["x-ms-query-source-authorization"], "finance-user-token")
            self.assertNotIn("x-ms-enable-elevated-read", request.headers)
            body = json.loads(request.content)
            self.assertIn("sensitivity_label_id ne null", body["knowledgeSourceParams"][0]["filterAddOn"])
            return httpx.Response(200, json={
                "response": [{"content": [{"text": "Untrusted aggregate IQ output"}]}],
                "references": [
                    {"type": "searchIndex", "sensitivityLabelInfo": {"labelId": LABEL},
                     "citationUrl": "https://private.test",
                     "sourceData": {"content": "Approved excerpt", "metadata_storage_name": "Finance.docx"}},
                    {"type": "searchIndex", "sourceData": {"content": "Preview excerpt",
                     "metadata_storage_name": "Plan.docx", "sensitivity_label_id": LABEL}},
                    {"type": "searchIndex", "sourceData": {"content": "Unlabeled excerpt"}},
                    {"type": "searchIndex", "sensitivityLabelInfo": {"labelId": "not-a-guid"},
                     "sourceData": {"content": "Invalid label excerpt", "sensitivity_label_id": LABEL}},
                    {"type": "searchIndex", "sensitivityLabelInfo": {"labelId": LABEL},
                     "sourceData": {"content": "Conflicting label excerpt",
                                    "sensitivity_label_id": "22345678-4321-4321-4321-123456789abc"}},
                ],
            })

        model = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(text="Approved answer")))
        async with self.client(httpx.MockTransport(search_response)) as search_http:
            search = PurviewSearch("https://example.search.windows.net", "protected", self.credential, search_http)
            server = build_protected_host(model, search)
            async with self.client(httpx.ASGITransport(app=server)) as client:
                response = await client.post(
                    "/responses", json={"input": "Question", "store": False}, headers={TOKEN_HEADER: "finance-user-token"}
                )
        self.assertEqual(response.json()["status"], "completed")
        self.assertIn("Approved answer", response.text)
        payload = json.loads(model.run.await_args.args[0])
        self.assertEqual(payload["documents"], [
            {"source": "Finance.docx", "excerpt": "Approved excerpt"},
            {"source": "Plan.docx", "excerpt": "Preview excerpt"},
        ])
        self.assertNotIn("finance-user-token", response.text)

    async def test_requires_delegation_and_stateless_input_before_contacting_search(self):
        model = SimpleNamespace(run=AsyncMock())
        search = SimpleNamespace(excerpts=AsyncMock())
        async with self.client(httpx.ASGITransport(app=build_protected_host(model, search))) as client:
            payload = {"input": "Question", "store": False}
            response = await client.post("/responses", json=payload)
            self.assertEqual(response.status_code, 401)
            for change in (
                {"store": True}, {"conversation": "conv_test"},
                {"background": True}, {"previous_response_id": "resp_test"},
            ):
                response = await client.post("/responses", json=payload | change, headers={TOKEN_HEADER: "a-token"})
                self.assertEqual(response.status_code, 400)
        search.excerpts.assert_not_awaited()
        model.run.assert_not_awaited()

    async def test_label_denial_returns_no_documents_without_asking_model(self):
        model = SimpleNamespace(run=AsyncMock())
        search = SimpleNamespace(excerpts=AsyncMock(return_value=[]))
        async with self.client(httpx.ASGITransport(app=build_protected_host(model, search))) as client:
            response = await client.post(
                "/responses", json={"input": "Question", "store": False}, headers={TOKEN_HEADER: "other-user-token"}
            )
        self.assertIn(NO_DOCUMENTS, response.text)
        model.run.assert_not_awaited()

    async def test_invalid_token_fails_request_without_answer_or_search_details(self):
        model = SimpleNamespace(run=AsyncMock())
        search = SimpleNamespace(excerpts=AsyncMock(side_effect=ProtectedSearchError("sensitive upstream message")))
        server = build_protected_host(model, search)
        async with self.client(httpx.ASGITransport(app=server, raise_app_exceptions=False)) as client:
            response = await client.post(
                "/responses", json={"input": "Question", "store": False}, headers={TOKEN_HEADER: "invalid-token"}
            )
        self.assertTrue(response.is_error or response.json().get("status") == "failed")
        self.assertNotIn("sensitive upstream message", response.text)
        model.run.assert_not_awaited()

    async def test_logs_distinguish_empty_result_from_failed_retrieval(self):
        def respond(references, status=200):
            def handler(request):
                if request.method == "GET":
                    return httpx.Response(200, json=DEFINITIONS[request.url.path])
                return httpx.Response(status, json={"references": references})
            return handler

        unlabeled = [{"type": "searchIndex", "sourceData": {"content": "Hidden"}}]
        async with self.client(httpx.MockTransport(respond(unlabeled))) as http:
            search = PurviewSearch("https://example.search.windows.net", "protected", self.credential, http)
            with self.assertLogs("purview_search", "INFO") as logs:
                self.assertEqual(await search.excerpts("Question", "delegated-token"), [])
        self.assertIn("retrieval_outcome=ok references=1 kept=0", logs.output[0])
        self.assertNotIn("Question", logs.output[0])

        server = build_protected_host(SimpleNamespace(run=AsyncMock()), SimpleNamespace(
            excerpts=AsyncMock(side_effect=ProtectedSearchError("down"))))
        async with self.client(httpx.ASGITransport(app=server, raise_app_exceptions=False)) as client:
            with self.assertLogs("purview_host", "WARNING") as logs:
                await client.post("/responses", json={"input": "Question", "store": False},
                                  headers={TOKEN_HEADER: "delegated-token"})
        self.assertIn("retrieval_outcome=failed", logs.output[0])

    async def test_wrong_schema_or_search_error_never_falls_back_to_unfiltered_search(self):
        targets = list(DEFINITIONS)[-2:]  # Search source and knowledge base
        source, kb = targets
        for invalid, status, expected_methods in (
            ({"/indexes/protected": SCHEMA | {"purviewEnabled": False}}, 200, ["GET"]),
            ({kb: {"knowledgeSources": [{"name": "unexpected"}]}}, 200, ["GET"] * 2),
            ({source: {"kind": "web"}}, 200, ["GET"] * 3),
            ({}, 401, ["GET"] * 3 + ["POST"]),
            ({}, 206, ["GET"] * 3 + ["POST"]),
            ({}, 502, ["GET"] * 3 + ["POST"]),
        ):
            with self.subTest(status=status):
                methods = []

                def search_response(request):
                    methods.append(request.method)
                    if request.method == "GET":
                        return httpx.Response(200, json=(DEFINITIONS | invalid)[request.url.path])
                    return httpx.Response(status, json={"error": "details not for the model"})

                async with self.client(httpx.MockTransport(search_response)) as http:
                    search = PurviewSearch("https://example.search.windows.net", "protected", self.credential, http)
                    with self.assertRaises(ProtectedSearchError):
                        await search.excerpts("Question", "delegated-token")
                self.assertEqual(methods, expected_methods)
