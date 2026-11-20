"""Format ranked sample-corpus results for the hosted agent."""

from collections.abc import Mapping

MAX_DOCUMENT_CHARS = 6000


def format_search_result(result: Mapping) -> str:
    content = result.get("content") or result.get("snippet") or result.get("chunk") or result.get("text") or ""
    # Extractive captions alone can omit the procedure even for a top-ranked document.
    if not content or len(content) > MAX_DOCUMENT_CHARS:
        captions = result.get("@search.captions") or []
        caption = " ".join(c.text for c in captions if c.text)
        if caption:
            if not content:
                content = caption
            elif caption not in content[:MAX_DOCUMENT_CHARS]:
                content = f"{caption}\n\n{content}"
    url = result.get("source_url") or result.get("metadata_storage_path")
    name = result.get("metadata_storage_name")
    header = f"[{name}]({url})" if name and url else f"[{name}]" if name else ""
    return "\n".join(part for part in (header, content[:MAX_DOCUMENT_CHARS]) if part)
