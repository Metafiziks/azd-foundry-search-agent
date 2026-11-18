"""Stateless retrieval against a Purview-enabled Azure AI Search index."""

import logging
import re
from uuid import UUID

import httpx
from azure.core.credentials_async import AsyncTokenCredential

API_VERSION = "2026-11-01-preview"
SEARCH_SCOPE = "https://search.azure.com/.default"
TOKEN_HEADER = "x-client-search-access-token"
LABEL_FIELD = "sensitivity_label_id"
logger = logging.getLogger(__name__)


class ProtectedSearchError(RuntimeError):
    """Protected retrieval could not be completed safely."""


def index_url(endpoint: str, index: str) -> str:
    endpoint = endpoint.rstrip("/")
    if not re.fullmatch(r"https://[a-z0-9][a-z0-9-]*\.search\.windows\.net", endpoint):
        raise ValueError("A public-cloud Azure AI Search HTTPS endpoint is required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", index):
        raise ValueError("A concrete Azure AI Search index name is required")
    return f"{endpoint}/indexes/{index}"


def is_purview_index(schema: dict, index: str) -> bool:
    fields = schema.get("fields")
    return (
        schema.get("name") == index
        and schema.get("purviewEnabled") is True
        and isinstance(fields, list)
        and any(
            isinstance(field, dict)
            and field.get("name") == LABEL_FIELD
            and field.get("type") == "Edm.String"
            and field.get("sensitivityLabelId") is True
            and field.get("filterable") is True
            and field.get("retrievable") is True
            for field in fields
        )
    )


def _has_label(document: dict) -> bool:
    value = document.get(LABEL_FIELD)
    if not isinstance(value, str):
        return False
    try:
        label = UUID(value)
        return str(label) == value.lower() and label.int != 0
    except ValueError:
        return False


class PurviewSearch:
    def __init__(
        self, endpoint: str, index: str, credential: AsyncTokenCredential, http: httpx.AsyncClient
    ) -> None:
        self.url = index_url(endpoint, index)
        self.index = index
        self.credential = credential
        self.http = http

    async def excerpts(self, question: str, user_token: str) -> list[dict[str, str]]:
        # Forward the opaque delegated token only to Search; Search validates it.
        if not user_token or not user_token.isascii() or not all(33 <= ord(c) <= 126 for c in user_token):
            raise ProtectedSearchError("A delegated Azure AI Search access token is required")

        app_token = await self.credential.get_token(SEARCH_SCOPE)
        headers = {"Authorization": f"Bearer {app_token.token}"}
        params = {"api-version": API_VERSION}
        try:
            schema = await self.http.get(self.url, params=params, headers=headers, follow_redirects=False)
            schema.raise_for_status()
            definition = schema.json()
            if not isinstance(definition, dict) or not is_purview_index(definition, self.index):
                raise ProtectedSearchError("The configured Search index is not Purview-enabled")

            response = await self.http.post(
                f"{self.url}/docs/search",
                params=params,
                headers={**headers, "x-ms-query-source-authorization": user_token},
                json={
                    "search": question,
                    "top": 5,
                    "queryType": "semantic",
                    "semanticConfiguration": "default",
                    "captions": "extractive",
                    "filter": f"{LABEL_FIELD} ne null and {LABEL_FIELD} ne ''",
                    "select": f"metadata_storage_name,content,{LABEL_FIELD}",
                },
                follow_redirects=False,
            )
            response.raise_for_status()
            documents = response.json()["value"]
        except httpx.HTTPStatusError as exc:
            logger.warning("Purview Search request denied or unavailable (HTTP %d)", exc.response.status_code)
            raise ProtectedSearchError("Protected document retrieval failed") from None
        except (httpx.RequestError, ValueError, KeyError) as exc:
            logger.warning("Purview Search request failed (%s)", type(exc).__name__)
            raise ProtectedSearchError("Protected document retrieval failed") from None

        if not isinstance(documents, list):
            raise ProtectedSearchError("Invalid protected Search response")

        results = []
        for doc in documents:
            if not isinstance(doc, dict) or not _has_label(doc):
                continue
            text = " ".join(
                caption["text"] for caption in (doc.get("@search.captions") or [])
                if isinstance(caption, dict) and isinstance(caption.get("text"), str)
            ) or doc.get("content")
            if isinstance(text, str) and text:
                # Do not expose private Blob URLs, label IDs or other index metadata.
                name = doc.get("metadata_storage_name")
                results.append({"source": name if isinstance(name, str) and name else "Document", "excerpt": text})
        return results
