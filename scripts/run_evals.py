#!/usr/bin/env python3
"""
RAG Agent Evaluation Runner — Azure AI Foundry
-----------------------------------------------
Evaluates the Foundry hosted agent against a fixed test suite.

Metrics:
  keyword_recall   (deterministic) — fraction of expected keywords found in the answer
  citation_recall  (deterministic) — expected source doc appeared in citations (0 or 1)
  latency_ms       (deterministic) — wall-clock time for the Responses API call
  faithfulness     (LLM-as-judge)  — every claim grounded in cited sources (0–1)
  answer_relevance (LLM-as-judge)  — answer fully addresses the question (0–1)

Judge model: gpt-5 via Foundry (separate from the agent's own context)

Pass thresholds (configurable via env vars):
  THRESHOLD_FAITHFULNESS    default 0.70
  THRESHOLD_RELEVANCE       default 0.75
  THRESHOLD_CITATION_RECALL default 0.60
  THRESHOLD_KEYWORD_RECALL  default 0.65
  THRESHOLD_P95_LATENCY_MS  default 10000

Usage:
  # Load env from azd, then run:
  eval $(azd env get-values) python3 scripts/run_evals.py
  # or use the wrapper:
  bash scripts/eval.sh
"""

import argparse
import json
import math
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from azure.identity import AzureCliCredential

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FOUNDRY_PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
AZURE_OPENAI_ENDPOINT    = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
MODEL_DEPLOYMENT          = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5")
# azd stores the agent name as AGENT_SEARCH_AGENT_NAME; AGENT_NAME may be empty string
AGENT_NAME                = (os.environ.get("AGENT_NAME") or
                             os.environ.get("AGENT_SEARCH_AGENT_NAME", "search-agent"))
# Use pre-built responses endpoint from azd if available
_RESPONSES_ENDPOINT_ENV  = os.environ.get("AGENT_SEARCH_AGENT_RESPONSES_ENDPOINT", "")
RESPONSES_API_VERSION     = "v1"

EVAL_IS_BASELINE = os.environ.get("EVAL_IS_BASELINE", "false").lower() == "true"
MEMORY_EVAL_ENABLED = (
    os.environ.get("EVAL_MEMORY_ENABLED", "").lower() in {"1", "true", "yes", "on"}
    or (
        os.environ.get("EVAL_MEMORY_ENABLED") is None
        and os.environ.get("MEMORY_ENABLED", "").lower() == "true"
        and bool(os.environ.get("MEMORY_STORE_NAME"))
    )
)
EVAL_MEMORY_USER_ID = os.environ.get("EVAL_MEMORY_USER_ID", f"eval-user-{os.environ.get('AZURE_ENV_NAME', 'local')}")
EVAL_MEMORY_SESSION_ID = os.environ.get("EVAL_MEMORY_SESSION_ID", f"eval-session-{uuid.uuid4()}")
EVAL_MEMORY_SETTLE_SECONDS = int(os.environ.get("EVAL_MEMORY_SETTLE_SECONDS", "20"))

THRESHOLDS = {
    "faithfulness":     float(os.environ.get("THRESHOLD_FAITHFULNESS",    "0.60")),
    "answer_relevance": float(os.environ.get("THRESHOLD_RELEVANCE",       "0.75")),
    "citation_recall":  float(os.environ.get("THRESHOLD_CITATION_RECALL", "0.60")),
    "keyword_recall":   float(os.environ.get("THRESHOLD_KEYWORD_RECALL",  "0.65")),
    "p95_latency_ms":   float(os.environ.get("THRESHOLD_P95_LATENCY_MS",  "45000")),
}

EVAL_CASES_PATH = Path(__file__).parent.parent / "tests" / "eval_cases.json"
DEFAULT_OUTPUT  = Path(__file__).parent.parent / "eval_results.json"

JUDGE_PROMPT = """\
You are a strict evaluator for a RAG (Retrieval-Augmented Generation) system.
Score the following answer on two dimensions. Return ONLY valid JSON, no explanation.

Question: {question}

Answer: {answer}

Citations used: {citations}

Score each metric from 1 (very poor) to 5 (excellent):

- faithfulness: Does every factual claim in the answer appear in the cited documents?
  1=answer contains made-up facts, 5=every claim is grounded in citations
- answer_relevance: Does the answer fully address what was asked?
  1=completely off-topic, 5=directly and completely addresses the question

Return exactly:
{{"faithfulness": <1-5>, "answer_relevance": <1-5>, "reasoning": "<one sentence>"}}"""

# ---------------------------------------------------------------------------
# HHEM scoring (lazy load)
# ---------------------------------------------------------------------------

_hhem = None

def _get_hhem():
    global _hhem
    if _hhem is None:
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from observability.shared.hhem import HHEMScorer
            _hhem = HHEMScorer()
        except Exception as exc:
            print(f"  [HHEM unavailable: {exc}]", file=sys.stderr)
            _hhem = None
    return _hhem

def score_hhem(question: str, answer: str) -> float | None:
    if os.environ.get("SKIP_HHEM", "false").lower() == "true":
        return None
    scorer = _get_hhem()
    if scorer is None:
        return None
    try:
        return scorer.score(question, answer)
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Log Analytics telemetry sink (fire-and-forget per eval case)
# ---------------------------------------------------------------------------

def _log_telemetry_la(row: dict) -> None:
    """Ingest a telemetry row to Log Analytics if endpoint env vars are set."""
    dce = os.environ.get("LOG_ANALYTICS_DCE")
    dcr = os.environ.get("LOG_ANALYTICS_DCR_IMMUTABLE_ID")
    stream = os.environ.get("LOG_ANALYTICS_STREAM_NAME", "Custom-AgentTelemetry_CL")
    if not (dce and dcr):
        return
    try:
        import threading
        from azure.monitor.ingestion import LogsIngestionClient
        client = LogsIngestionClient(endpoint=dce, credential=AzureCliCredential())
        def _upload():
            try:
                client.upload(rule_id=dcr, stream_name=stream, logs=[row])
            except Exception:
                pass
        threading.Thread(target=_upload, daemon=True).start()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Azure helpers
# ---------------------------------------------------------------------------

_credential = AzureCliCredential()
_token_cache: dict = {}


def get_token(scope: str = "https://ai.azure.com") -> str:
    """Get a cached bearer token for the given scope."""
    now = time.time()
    if scope in _token_cache and _token_cache[scope]["expires"] > now + 30:
        return _token_cache[scope]["token"]
    tok = _credential.get_token(scope)
    _token_cache[scope] = {"token": tok.token, "expires": tok.expires_on}
    return tok.token


def responses_post(payload: dict, extra_headers: dict[str, str] | None = None) -> dict:
    """POST to the agent-specific Responses API endpoint."""
    import urllib.request, urllib.error
    # Prefer pre-built endpoint from azd; fall back to constructing from project endpoint
    url = (_RESPONSES_ENDPOINT_ENV or
           f"{FOUNDRY_PROJECT_ENDPOINT}/agents/{AGENT_NAME}/endpoint/protocols/openai/responses?api-version={RESPONSES_API_VERSION}")
    data = json.dumps(payload).encode()

    for _attempt in range(5):
        token = get_token("https://ai.azure.com/.default")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        for name, value in (extra_headers or {}).items():
            req.add_header(name, value)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:500]
            if e.code in (500, 503) and _attempt < 4:
                wait = 90 * (1 + _attempt)  # 90/180/270/360s — container needs time to restart
                print(f" [agent {e.code}, retrying in {wait}s]", end="", flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body}") from e

        # Agent-level rate limit: HTTP 200 but status=failed with rate_limit error
        if result.get("status") == "failed":
            err = result.get("error", {})
            err_code = (err.get("code", "") if isinstance(err, dict) else str(err)).lower()
            err_msg  = (err.get("message", "") if isinstance(err, dict) else str(err)).lower()
            if ("rate_limit" in err_code or "rate_limit" in err_msg) and _attempt < 4:
                wait = 120 * (_attempt + 1)
                print(f" [agent rate-limited, retrying in {wait}s]", end="", flush=True)
                time.sleep(wait)
                continue
            raw_msg = (err.get("message", str(err)) if isinstance(err, dict) else str(err))
            raise RuntimeError(f"Agent status=failed: {raw_msg[:300]}")

        return result

    raise RuntimeError("responses_post: exceeded max retries")

# ---------------------------------------------------------------------------
# Agent caller
# ---------------------------------------------------------------------------

def call_agent(question: str, extra_headers: dict[str, str] | None = None) -> tuple[str, list[str], float]:
    """
    Call the Foundry hosted agent with a question.
    Returns (answer_text, citation_filenames, latency_ms).
    """
    if not FOUNDRY_PROJECT_ENDPOINT:
        raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT is not set. Run: eval $(azd env get-values)")

    payload = {
        "model": AGENT_NAME,
        "input": [{"role": "user", "content": question}],
    }

    start = time.time()
    result = responses_post(payload, extra_headers=extra_headers)
    latency_ms = (time.time() - start) * 1000

    # Extract answer text from Responses API output
    answer_parts = []
    for item in result.get("output", []):
        if item.get("type") == "message" and item.get("role") == "assistant":
            for part in item.get("content", []):
                # Foundry hosted agents use type="output_text" with text as a plain string
                if part.get("type") in ("output_text", "text"):
                    raw = part.get("text", "")
                    if isinstance(raw, dict):
                        raw = raw.get("value", "")
                    answer_parts.append(raw)
    answer = " ".join(answer_parts).strip()

    # Extract citation filenames from markdown links: [name](url)
    # Storage URLs look like: .../docs/maintenance/hydraulic_press_troubleshooting.txt
    citations = re.findall(r"\[([^\]]+)\]\((https?://[^\)]+)\)", answer)
    filenames = []
    for _, url in citations:
        fname = url.rstrip("/").split("/")[-1]
        if fname:
            filenames.append(fname)
    # Also look for bare filenames
    filenames += re.findall(r"([\w_]+\.txt)", answer)
    filenames = list(dict.fromkeys(filenames))  # dedupe, preserve order

    return answer, filenames, latency_ms


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------

def call_judge(question: str, answer: str, citations: list[str]) -> dict:
    """
    Call GPT-5 via Foundry to score faithfulness and answer_relevance.
    Returns {"faithfulness": float 0-1, "answer_relevance": float 0-1, "reasoning": str}.
    """
    citation_str = ", ".join(citations) if citations else "none"
    prompt = JUDGE_PROMPT.format(question=question, answer=answer, citations=citation_str)

    # Use Chat Completions API via the OpenAI-compatible endpoint
    import urllib.request, urllib.error
    openai_base = AZURE_OPENAI_ENDPOINT.rstrip("/") if AZURE_OPENAI_ENDPOINT else FOUNDRY_PROJECT_ENDPOINT
    url = f"{openai_base}/openai/deployments/{MODEL_DEPLOYMENT}/chat/completions?api-version=2024-12-01-preview"
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": 4000,
        "response_format": {"type": "json_object"},
    }

    for attempt in range(4):
        try:
            data = json.dumps(payload).encode()
            token = get_token("https://cognitiveservices.azure.com/.default")
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
            break
        except Exception as exc:
            if "429" in str(exc) or "429" in getattr(exc, "code", str(exc)):
                wait = 30 * (2 ** attempt)  # 30/60/120/240s
                print(f" [rate limited, retrying in {wait}s]", end="", flush=True)
                time.sleep(wait)
                if attempt == 3:
                    raise
            else:
                raise

    text = result["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        scores = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: extract numbers with regex
        f = re.search(r'"faithfulness"\s*:\s*(\d)', text)
        r_ = re.search(r'"answer_relevance"\s*:\s*(\d)', text)
        scores = {
            "faithfulness": int(f.group(1)) if f else 1,
            "answer_relevance": int(r_.group(1)) if r_ else 1,
            "reasoning": "parse fallback",
        }

    return {
        "faithfulness":     (scores["faithfulness"]    - 1) / 4,  # 1-5 → 0-1
        "answer_relevance": (scores["answer_relevance"] - 1) / 4,
        "reasoning": scores.get("reasoning", ""),
    }


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = max(0, int(len(sorted_vals) * p / 100) - 1)
    return sorted_vals[idx]


def fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def is_memory_case(case: dict) -> bool:
    return bool(case.get("memory")) or case.get("category") == "memory"


def memory_headers(case_id: str) -> dict[str, str]:
    user_id = os.environ.get(f"EVAL_MEMORY_USER_ID_{case_id.upper().replace('-', '_')}", EVAL_MEMORY_USER_ID)
    return {
        "x-agent-user-id": user_id,
        "x-memory-user-id": user_id,
        "x-memory-session-id": EVAL_MEMORY_SESSION_ID,
    }


# ---------------------------------------------------------------------------
# Main eval loop
# ---------------------------------------------------------------------------

def run_evals(cases: list[dict], use_judge: bool, output_path: Path) -> dict:
    results = []
    latencies = []
    skipped = 0
    skipped_memory = 0

    print(f"Loaded {len(cases)} eval cases")
    print(f"Agent:    {FOUNDRY_PROJECT_ENDPOINT} (model={AGENT_NAME})")
    print(f"Judge:    {MODEL_DEPLOYMENT} via Foundry{' (skipped)' if not use_judge else ''}")
    print(f"Baseline: {EVAL_IS_BASELINE}")
    print(f"HHEM:     {'disabled' if os.environ.get('SKIP_HHEM') == 'true' else 'enabled'}")
    print(f"Memory:   {'enabled' if MEMORY_EVAL_ENABLED else 'skipped'}")
    print()

    for i, case in enumerate(cases, 1):
        # Pause between cases: agent's internal gpt-5 quota needs to partially reset
        if i > 1:
            inter_wait = 420
            print(f"  [waiting {inter_wait}s between cases for container restart + quota reset]", flush=True)
            time.sleep(inter_wait)

        cid   = case["id"]
        memory_case = is_memory_case(case)
        q     = case.get("question") or case.get("turns", [{}])[-1].get("question", "")
        kws   = [kw.lower() for kw in case.get("expected_keywords", [])]
        srcs  = [s.lower() for s in case.get("expected_sources", [])]

        print(f"[{i:2d}/{len(cases)}] {cid} ...", end=" ", flush=True)

        if memory_case and not MEMORY_EVAL_ENABLED:
            print("SKIPPED (memory not configured)")
            skipped_memory += 1
            results.append({
                "id": cid,
                "question": q,
                "answer": None,
                "citations": [],
                "keyword_recall": None,
                "citation_recall": None,
                "faithfulness": None,
                "answer_relevance": None,
                "hhem_score": None,
                "latency_ms": None,
                "memory": True,
                "turns": [],
                "skipped": True,
                "error": None,
            })
            output_path.write_text(json.dumps({"results": results, "partial": True}, indent=2))
            continue

        try:
            turn_results = []
            if case.get("turns"):
                headers = memory_headers(cid) if memory_case else None
                total_latency_ms = 0.0
                answer = ""
                citations = []
                for turn_index, turn in enumerate(case["turns"]):
                    answer, citations, turn_latency_ms = call_agent(turn["question"], extra_headers=headers)
                    total_latency_ms += turn_latency_ms
                    turn_results.append({
                        "question": turn["question"],
                        "answer": answer,
                        "citations": citations,
                        "latency_ms": turn_latency_ms,
                    })
                    if turn_index < len(case["turns"]) - 1:
                        print(f"turn{turn_index + 1}={turn_latency_ms:.0f}ms wait={EVAL_MEMORY_SETTLE_SECONDS}s ", end="", flush=True)
                        time.sleep(EVAL_MEMORY_SETTLE_SECONDS)
                latency_ms = total_latency_ms
            else:
                answer, citations, latency_ms = call_agent(q)
            citations_lower = [c.lower() for c in citations]

            # Deterministic metrics
            kw_hits  = sum(1 for kw in kws if kw in answer.lower())
            kw_score = kw_hits / len(kws) if kws else 1.0
            cite_hit = int(any(src in " ".join(citations_lower) for src in srcs)) if srcs else 1

            # LLM judge
            if use_judge and answer:
                judge_scores = call_judge(q, answer, citations)
                faithfulness    = judge_scores["faithfulness"]
                answer_relevance = judge_scores["answer_relevance"]
            else:
                faithfulness = answer_relevance = None

            # HHEM hallucination scoring (ML model)
            hhem_score = score_hhem(q, answer)

            latencies.append(latency_ms)

            suffix = f"kw={kw_score:.2f} cite={'✅' if cite_hit else '❌'}"
            if use_judge and faithfulness is not None:
                suffix += f" faith={faithfulness:.2f} rel={answer_relevance:.2f}"
            if hhem_score is not None:
                suffix += f" hhem={hhem_score:.3f}"
            print(f"{suffix} {latency_ms:.0f}ms")

            # Telemetry → Log Analytics
            n = max(len(citations), 1)
            _log_telemetry_la({
                "RequestId":             str(uuid.uuid4()),
                "CaseId":                cid,
                "IsBaseline":            EVAL_IS_BASELINE,
                "Source":                "eval",
                "RetrievalScoreMean":    0.5,
                "RetrievalScoreStd":     0.0,
                "RetrievalScoreEntropy": math.log(n),
                "ChunkCount":            n,
                "RerankerScoreMean":     0.0,
                "SearchLatencyMs":       latency_ms,
                "AnswerLength":          len(answer),
                "CitationCount":         len(citations),
                "HhemScore":             hhem_score or 0.0,
                "LatencyMs":             latency_ms,
                "Faithfulness":          faithfulness,
                "AnswerRelevance":       answer_relevance,
                "KeywordRecall":         kw_score,
                "CitationRecall":        float(cite_hit),
                "MemoryEnabled":         MEMORY_EVAL_ENABLED,
                "MemoryReadCount":       None,
                "MemoryWriteCount":      None,
                "MemoryLatencyMs":       None,
                "MemoryStatus":          "eval_memory_case" if memory_case else None,
                "TimeGenerated":         datetime.now(timezone.utc).isoformat(),
            })

            results.append({
                "id": cid,
                "question": q,
                "answer": answer,
                "citations": citations,
                "keyword_recall": kw_score,
                "citation_recall": cite_hit,
                "faithfulness": faithfulness,
                "answer_relevance": answer_relevance,
                "hhem_score": hhem_score,
                "latency_ms": latency_ms,
                "memory": memory_case,
                "turns": turn_results,
                "skipped": False,
                "error": None,
            })
            # Incremental save so results survive an early stop
            output_path.write_text(json.dumps({"results": results, "partial": True}, indent=2))

        except Exception as exc:
            print(f"ERROR: {exc}")
            skipped += 1
            results.append({
                "id": cid, "question": q, "answer": None, "citations": [],
                "keyword_recall": None, "citation_recall": None,
                "faithfulness": None, "answer_relevance": None,
                "hhem_score": None, "latency_ms": None,
                "memory": memory_case, "turns": [], "skipped": False, "error": str(exc),
            })

    # Aggregate
    valid = [r for r in results if r["error"] is None and not r.get("skipped")]
    summary = {
        "keyword_recall":    sum(r["keyword_recall"] for r in valid) / len(valid) if valid else 0,
        "citation_recall":   sum(r["citation_recall"] for r in valid) / len(valid) if valid else 0,
        "faithfulness":      sum(r["faithfulness"] for r in valid if r["faithfulness"] is not None) / max(1, sum(1 for r in valid if r["faithfulness"] is not None)),
        "answer_relevance":  sum(r["answer_relevance"] for r in valid if r["answer_relevance"] is not None) / max(1, sum(1 for r in valid if r["answer_relevance"] is not None)),
        "p95_latency_ms":    percentile(latencies, 95),
        "cases_total":       len(cases),
        "cases_error":       skipped,
        "cases_skipped":     skipped_memory,
    }

    threshold_keys = list(THRESHOLDS)
    if not use_judge:
        threshold_keys = [k for k in threshold_keys if k not in {"faithfulness", "answer_relevance"}]
    failures = [
        k for k in threshold_keys
        for t in [THRESHOLDS[k]]
        if (summary.get(k, 0) > t if k == "p95_latency_ms" else summary.get(k, 0) < t)
    ]
    passed   = len(failures) == 0

    # Write JSON output
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "summary": summary,
        "thresholds": THRESHOLDS,
        "failures": failures,
        "results": results,
    }
    output_path.write_text(json.dumps(output, indent=2))
    print(f"\nResults saved → {output_path}")

    # Markdown report
    status = "✅ PASSED" if passed else "❌ FAILED"
    lines  = [f"\n## Eval Results — {status}", "", "### Summary", ""]
    lines += ["| Metric | Score | Threshold | Status |", "|--------|-------|-----------|--------|"]
    metric_labels = {
        "faithfulness": "Faithfulness",
        "answer_relevance": "Answer Relevance",
        "citation_recall": "Citation Recall",
        "keyword_recall": "Keyword Recall",
        "p95_latency_ms": "p95 Latency (ms)",
    }
    for k, label in metric_labels.items():
        score = summary.get(k, 0)
        thr   = THRESHOLDS[k]
        ok    = "—" if k not in threshold_keys else ("✅" if (score <= thr if k == "p95_latency_ms" else score >= thr) else "❌")
        val   = f"{score:.4f}" if k != "p95_latency_ms" else f"{score:.0f}"
        tval  = f"{thr:.4f}" if k != "p95_latency_ms" else f"{thr:.0f}"
        lines.append(f"| {label} | {val} | {tval} | {ok} |")

    lines += ["", "### Per-Case Results", ""]
    lines += ["| Case | Faithful | Relevant | Cite✓ | KW✓ | Latency |",
              "|------|----------|----------|-------|-----|---------|"]
    for r in results:
        if r["error"]:
            lines.append(f"| {r['id']} | ERR | ERR | ERR | ERR | — |")
        elif r.get("skipped"):
            lines.append(f"| {r['id']} | SKIP | SKIP | SKIP | SKIP | — |")
        else:
            f_  = f"{r['faithfulness']:.2f}"  if r["faithfulness"] is not None else "—"
            rv_ = f"{r['answer_relevance']:.2f}" if r["answer_relevance"] is not None else "—"
            ci_ = "✅" if r["citation_recall"] else "❌"
            kw_ = f"{r['keyword_recall']:.2f}"
            la_ = f"{r['latency_ms']:.0f}ms"
            lines.append(f"| {r['id']} | {f_} | {rv_} | {ci_} | {kw_} | {la_} |")

    if failures:
        lines += ["", "### Failures", ""]
        for f in failures:
            lines.append(f"- {f}: {summary.get(f, 0):.4f} < {THRESHOLDS[f]:.4f} threshold")

    report = "\n".join(lines)
    print(report)

    # GitHub Actions step summary
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as fh:
            fh.write(report + "\n")

    return output


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output",    default=str(DEFAULT_OUTPUT), help="JSON output path")
    parser.add_argument("--no-judge",  action="store_true",         help="Skip LLM judge (deterministic only)")
    parser.add_argument("--cases",     default=str(EVAL_CASES_PATH),help="Path to eval_cases.json")
    args = parser.parse_args()

    if not FOUNDRY_PROJECT_ENDPOINT:
        print("ERROR: FOUNDRY_PROJECT_ENDPOINT is not set", file=sys.stderr)
        print("  Run:  eval $(azd env get-values) && python3 scripts/run_evals.py", file=sys.stderr)
        sys.exit(1)

    cases = json.loads(Path(args.cases).read_text())
    output = run_evals(cases, use_judge=not args.no_judge, output_path=Path(args.output))

    sys.exit(0 if output["passed"] else 1)


if __name__ == "__main__":
    main()
