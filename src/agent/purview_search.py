"""Stateless Foundry IQ retrieval from one Purview-enabled Search index."""

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


def _has_label(value: object) -> bool:
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
        self.source = f"{index}-source"
        self.source_url = f"{endpoint.rstrip('/')}/knowledgesources/{self.source}"
        self.kb_url = f"{endpoint.rstrip('/')}/knowledgebases/{index}-kb"
        self.credential = credential
        self.http = http

    async def _verify_source(self, params: dict, headers: dict) -> None:
        async def read(url: str) -> dict:
            response = await self.http.get(url, params=params, headers=headers, follow_redirects=False)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ProtectedSearchError("Invalid protected Foundry IQ configuration")
            return result

        schema = await read(self.url)
        if not is_purview_index(schema, self.index):
            raise ProtectedSearchError("The configured Search index is not Purview-enabled")
        kb = await read(self.kb_url)
        sources = kb.get("knowledgeSources")
        if (
            not isinstance(sources, list)
            or len(sources) != 1
            or not isinstance(sources[0], dict)
            or sources[0].get("name") != self.source
        ):
            raise ProtectedSearchError("Foundry IQ must use only the configured protected source")
        source = await read(self.source_url)
        settings = source.get("searchIndexParameters")
        if (
            source.get("kind") != "searchIndex"
            or not isinstance(settings, dict)
            or settings.get("searchIndexName") != self.index
        ):
            raise ProtectedSearchError("Foundry IQ source does not target the protected index")

    async def excerpts(self, question: str, user_token: str) -> list[dict[str, str]]:
        # Forward the opaque delegated token only to Search; Search validates it.
        if not user_token or not user_token.isascii() or not all(33 <= ord(c) <= 126 for c in user_token):
            raise ProtectedSearchError("A delegated Azure AI Search access token is required")

        app_token = await self.credential.get_token(SEARCH_SCOPE)
        headers = {"Authorization": f"Bearer {app_token.token}"}
        params = {"api-version": API_VERSION}
        try:
            await self._verify_source(params, headers)
            response = await self.http.post(
                f"{self.kb_url}/retrieve",
                params=params,
                headers={**headers, "x-ms-query-source-authorization": user_token},
                json={
                    "intents": [{"type": "semantic", "search": question}],
                    "retrievalReasoningEffort": {"kind": "minimal"},
                    "outputMode": "extractiveData",
                    "maxRuntimeInSeconds": 25,
                    "maxOutputDocuments": 5,
                    "knowledgeSourceParams": [{
                        "kind": "searchIndex",
                        "knowledgeSourceName": self.source,
                        "failOnError": True,
                        "includeReferences": True,
                        "includeReferenceSourceData": True,
                        "filterAddOn": f"{LABEL_FIELD} ne null and {LABEL_FIELD} ne ''",
                    }],
                },
                follow_redirects=False,
            )
            response.raise_for_status()
            if response.status_code != 200:
                logger.warning("Foundry IQ returned incomplete retrieval (HTTP %d)", response.status_code)
                raise ProtectedSearchError("Protected document retrieval was incomplete")
            result = response.json()
            if not isinstance(result, dict) or result.get("error"):
                raise ProtectedSearchError("Invalid protected Foundry IQ response")
            references = result["references"]
        except httpx.HTTPStatusError as exc:
            logger.warning("Purview Search request denied or unavailable (HTTP %d)", exc.response.status_code)
            raise ProtectedSearchError("Protected document retrieval failed") from None
        except (httpx.RequestError, ValueError, KeyError) as exc:
            logger.warning("Purview Search request failed (%s)", type(exc).__name__)
            raise ProtectedSearchError("Protected document retrieval failed") from None

        if not isinstance(references, list):
            raise ProtectedSearchError("Invalid protected Foundry IQ references")

        results = []
        # Ground only from individually labeled references, not aggregate IQ output.
        for reference in references[:5]:
            if not isinstance(reference, dict) or reference.get("type") != "searchIndex":
                continue
            label = reference.get("sensitivityLabelInfo")
            doc = reference.get("sourceData")
            mapped_label = doc.get(LABEL_FIELD) if isinstance(doc, dict) else None
            if "sensitivityLabelInfo" in reference:
                label_id = label.get("labelId") if isinstance(label, dict) else None
                if not _has_label(label_id) or (
                    mapped_label is not None
                    and (not _has_label(mapped_label) or mapped_label.lower() != label_id.lower())
                ):
                    continue
            elif not _has_label(mapped_label):
                # The IQ preview can return the annotated index field without the reference wrapper.
                continue
            if not isinstance(doc, dict):
                raise ProtectedSearchError("Labeled Foundry IQ reference has no source data")
            text = doc.get("content")
            if isinstance(text, str) and text:
                name = doc.get("metadata_storage_name")
                results.append({
                    "source": name if isinstance(name, str) and name else "Document",
                    "excerpt": text[:6000],
                })
        # Proof the search ran: an empty list below means "ran, nothing allowed", never "unreachable"
        # (failures raise above). Counts only; no query or document content is logged.
        logger.info("retrieval_outcome=ok references=%d kept=%d", len(references), len(results))
        return results
