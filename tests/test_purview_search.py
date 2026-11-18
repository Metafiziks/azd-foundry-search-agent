import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError

import httpx

from purview_host import NO_DOCUMENTS, build_protected_host
from purview_search import ProtectedSearchError, PurviewSearch, TOKEN_HEADER
from setup_purview_index import definitions, deploy_search

LABEL = "12345678-4321-4321-4321-123456789abc"
SCHEMA = definitions("protected", "/storage-id", "private-docs")[0][1]


class ProtectedProvisioningTests(unittest.TestCase):
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
                return httpx.Response(200, json=SCHEMA)
            self.assertEqual(request.headers["x-ms-query-source-authorization"], "finance-user-token")
            self.assertNotIn("x-ms-enable-elevated-read", request.headers)
            body = json.loads(request.content)
            self.assertIn("sensitivity_label_id ne null", body["filter"])
            return httpx.Response(200, json={"value": [
                {"content": "Approved excerpt", "metadata_storage_name": "Finance.docx",
                 "source_url": "https://private.test", "sensitivity_label_id": LABEL},
                {"content": "Unlabeled excerpt"},
                {"content": "Invalid label excerpt", "sensitivity_label_id": "not-a-guid"},
            ]})

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
        self.assertEqual(payload["documents"], [{"source": "Finance.docx", "excerpt": "Approved excerpt"}])
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

    async def test_wrong_schema_or_search_error_never_falls_back_to_unfiltered_search(self):
        for schema, status, expected_methods in (
            (SCHEMA | {"purviewEnabled": False}, 200, ["GET"]),
            (SCHEMA, 401, ["GET", "POST"]),
            (SCHEMA, 502, ["GET", "POST"]),
        ):
            with self.subTest(status=status):
                methods = []

                def search_response(request):
                    methods.append(request.method)
                    if request.method == "GET":
                        return httpx.Response(200, json=schema)
                    return httpx.Response(status, json={"error": "details not for the model"})

                async with self.client(httpx.MockTransport(search_response)) as http:
                    search = PurviewSearch("https://example.search.windows.net", "protected", self.credential, http)
                    with self.assertRaises(ProtectedSearchError):
                        await search.excerpts("Question", "delegated-token")
                self.assertEqual(methods, expected_methods)
