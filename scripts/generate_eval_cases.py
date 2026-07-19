#!/usr/bin/env python3
"""
Auto-generate eval cases from the docs/ directory.
--------------------------------------------------
Reads every .txt file under docs/, uses GPT-5 to generate 2 Q&A test cases
per document, and writes tests/eval_cases.json in the standard eval format.

Run this whenever docs change to keep the eval suite in sync:

  eval $(azd env get-values)
  python3 scripts/generate_eval_cases.py            # writes tests/eval_cases.json
  python3 scripts/generate_eval_cases.py --dry-run  # prints cases, doesn't write

Output schema (matches tests/eval_cases.json):
  id               — kebab-case identifier
  category         — docs subdirectory name (maintenance, safety, quality, ...)
  question         — a question a worker on the floor might actually ask
  expected_keywords — 4-6 key phrases that must appear in a correct answer
  expected_sources  — [filename.txt] used to score citation recall
  turns             — optional multi-turn memory eval sequence
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from azure.identity import AzureCliCredential

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FOUNDRY_PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
AZURE_OPENAI_ENDPOINT     = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
MODEL_DEPLOYMENT          = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5")
RESPONSES_API_VERSION     = "2025-04-01-preview"
DOCS_DIR                  = Path(__file__).parent.parent / "docs"
OUTPUT_PATH               = Path(__file__).parent.parent / "tests" / "eval_cases.json"

MEMORY_EVAL_CASE = {
    "id": "memory-preference-recall",
    "category": "memory",
    "memory": True,
    "turns": [
        {
            "question": (
                "Please remember this for future manufacturing support: my preferred "
                "maintenance shift is second shift and my preferred unit label is Line 2."
            )
        },
        {
            "question": (
                "For my future manufacturing support requests, which maintenance shift "
                "and unit label should you use for me?"
            )
        },
    ],
    "expected_keywords": ["second shift", "Line 2"],
    "expected_sources": [],
}

GENERATOR_PROMPT = """\
You are writing test cases for a RAG evaluation suite.

Below is a manufacturing procedure document named "{filename}":

---
{content}
---

Generate exactly 2 test cases based on this document. Each test case must be a
question that a manufacturing floor worker or supervisor might realistically ask.

Rules:
- Questions must be specific enough that the document above is the clear source
  (e.g. "hydraulic press" not just "press", "lockout tagout" not just "safety")
- Questions must be answerable entirely from this document — no general knowledge
- expected_keywords must be 4-6 short, distinct phrases (1-3 words each) that will
  appear verbatim (case-insensitive) in any correct answer to this question.
  Use the most specific single noun or short verb phrase from the document.
  Avoid long multi-word phrases, full sentences, or phrases that the agent might
  paraphrase instead of quote.
- expected_sources must be exactly ["{filename}"]

Return ONLY a valid JSON object with a "cases" key — no markdown, no explanation:
{{
  "cases": [
    {{
      "id": "<kebab-case-id>",
      "category": "{category}",
      "question": "<question text>",
      "expected_keywords": ["<phrase1>", "<phrase2>", "<phrase3>", "<phrase4>"],
      "expected_sources": ["{filename}"]
    }},
    {{
      "id": "<kebab-case-id-2>",
      "category": "{category}",
      "question": "<question text>",
      "expected_keywords": ["<phrase1>", "<phrase2>", "<phrase3>", "<phrase4>"],
      "expected_sources": ["{filename}"]
    }}
  ]
}}"""

# ---------------------------------------------------------------------------
# Azure helpers
# ---------------------------------------------------------------------------

_credential = AzureCliCredential()


def get_token() -> str:
    tok = _credential.get_token("https://cognitiveservices.azure.com/.default")
    return tok.token


def chat_complete(prompt: str) -> str:
    """Call GPT-5 chat completions via Azure OpenAI deployment endpoint."""
    openai_base = AZURE_OPENAI_ENDPOINT if AZURE_OPENAI_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
    url = f"{openai_base}/openai/deployments/{MODEL_DEPLOYMENT}/chat/completions?api-version=2024-12-01-preview"
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": 8000,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload).encode()
    token = get_token()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    msg_content = result["choices"][0]["message"]["content"] or ""
    if not msg_content.strip():
        finish = result["choices"][0].get("finish_reason", "unknown")
        raise RuntimeError(f"Empty model response (finish_reason={finish}; likely reasoning tokens exhausted)")
    return msg_content.strip()

# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def generate_cases_for_doc(doc_path: Path) -> list[dict]:
    """Generate 2 eval cases for a single document using GPT-5."""
    content  = doc_path.read_text()
    filename = doc_path.name
    category = doc_path.parent.name

    prompt = GENERATOR_PROMPT.format(
        filename=filename,
        category=category,
        content=content,
    )

    for attempt in range(4):
        try:
            text = chat_complete(prompt)
            # Strip accidental markdown fences
            text = re.sub(r"^```json\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            # The response_format json_object wraps in an object — extract the array
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                # Model returned {"cases": [...]} or similar wrapper
                cases = next(v for v in parsed.values() if isinstance(v, list))
            else:
                cases = parsed
            for c in cases:
                assert "id" in c and "question" in c and "expected_keywords" in c
            return cases
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                wait = 15 * (2 ** attempt)
                print(f"    Rate limited — waiting {wait}s...", flush=True)
                time.sleep(wait)
            else:
                print(f"    HTTP {exc.code}: {exc.read().decode()[:200]}", file=sys.stderr)
                return []
        except Exception as exc:
            print(f"    ERROR for {filename}: {exc}", file=sys.stderr)
            return []

    print(f"    Giving up on {filename} after 4 attempts", file=sys.stderr)
    return []


def deduplicate_ids(cases: list[dict]) -> list[dict]:
    seen = {}
    result = []
    for c in cases:
        base = c["id"]
        if base in seen:
            seen[base] += 1
            c["id"] = f"{base}-{seen[base]}"
        else:
            seen[base] = 0
        result.append(c)
    return result

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate eval cases from docs/")
    parser.add_argument("--dry-run", action="store_true", help="Print cases, don't write")
    args = parser.parse_args()

    if not FOUNDRY_PROJECT_ENDPOINT:
        print("ERROR: FOUNDRY_PROJECT_ENDPOINT is not set.", file=sys.stderr)
        print("  Run: eval $(azd env get-values)", file=sys.stderr)
        sys.exit(1)

    if not DOCS_DIR.exists():
        print(f"ERROR: docs/ directory not found at {DOCS_DIR}", file=sys.stderr)
        sys.exit(1)

    doc_files = sorted(DOCS_DIR.rglob("*.txt"))
    if not doc_files:
        print("No .txt files found under docs/", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(doc_files)} document(s) — generating 2 cases each...")
    print(f"Model: {MODEL_DEPLOYMENT}")
    print()

    all_cases = []
    for doc_path in doc_files:
        rel = doc_path.relative_to(DOCS_DIR.parent)
        print(f"  {rel} ...", end=" ", flush=True)
        cases = generate_cases_for_doc(doc_path)
        print(f"{len(cases)} cases generated")
        all_cases.extend(cases)
        time.sleep(1)

    all_cases = deduplicate_ids(all_cases)
    all_cases.append(MEMORY_EVAL_CASE)
    print(f"\nTotal: {len(all_cases)} eval cases")

    if args.dry_run:
        print(json.dumps(all_cases, indent=2))
        return

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(all_cases, indent=2))
    print(f"Written → {OUTPUT_PATH}")
    print("\nRun evals with:\n  bash scripts/eval.sh")


if __name__ == "__main__":
    main()
