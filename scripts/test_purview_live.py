#!/usr/bin/env python3
"""Verify protected ingestion and retrieval through a deployed Foundry Responses endpoint."""

import argparse
import re
import sys
import time
from getpass import getpass
from urllib.parse import quote

import httpx

NO_DOCUMENTS = "I could not find information about that in the available documents."


def response_text(body):
    return "\n".join(
        part["text"]
        for item in body.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )


def run_tests(http, args, backend_token, allowed_token, denied_token, expired_token):
    endpoint = (
        f"https://{args.account}.services.ai.azure.com/api/projects/{args.project}"
        "/agents/search-agent/endpoint/protocols/openai/responses"
    )

    def invoke(case, oid, token, question, store=False):
        headers = {"Authorization": f"Bearer {backend_token}", "x-ms-user-identity": oid}
        if token:
            headers["x-client-search-access-token"] = token
        start = time.monotonic()
        response = http.post(
            endpoint, headers=headers, params={"api-version": "v1"},
            json={"input": question, "store": store, "background": False},
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        print(f"{case}: HTTP {response.status_code}, response={body.get('status')}, "
              f"{time.monotonic() - start:.2f}s", flush=True)
        return response.status_code, body

    status, body = invoke("approved", args.allowed_oid, allowed_token, args.question)
    if status != 200 or body.get("status") != "completed":
        raise RuntimeError("The approved request did not complete")
    answer = response_text(body).casefold()
    if args.expected.casefold() not in answer or args.labeled_blob.split("/")[-1].casefold() not in answer:
        raise RuntimeError("The approved answer must contain the test value and cite the labeled file")

    for case, oid, token, question in (
        ("denied", args.denied_oid, denied_token, args.question),
        ("unlabeled", args.allowed_oid, allowed_token, args.unlabeled_question),
    ):
        status, body = invoke(case, oid, token, question)
        if status != 200 or body.get("status") != "completed" or response_text(body).strip() != NO_DOCUMENTS:
            raise RuntimeError(f"{case}: expected a completed response with no available documents")

    status, body = invoke("missing token", args.allowed_oid, None, args.question)
    if status not in (401, 403) or response_text(body):
        raise RuntimeError("Missing delegation was not rejected")
    status, body = invoke("expired token", args.allowed_oid, expired_token, args.question)
    if (status not in (401, 403) and body.get("status") != "failed") or response_text(body):
        raise RuntimeError("Expired delegation was not rejected")
    status, body = invoke("stateful input", args.allowed_oid, allowed_token, args.question, store=True)
    if status != 400 or response_text(body):
        raise RuntimeError("Stateful input was not rejected")

    # Send no credentials or user headers to Blob Storage.
    for blob in (args.labeled_blob, args.unlabeled_blob):
        response = http.head(f"https://{args.storage_account}.blob.core.windows.net/docs/{quote(blob)}")
        print(f"anonymous Blob read: HTTP {response.status_code}", flush=True)
        if response.status_code not in (401, 403, 404, 409):
            raise RuntimeError("Anonymous Blob reads were not denied")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("account", "project", "storage-account", "allowed-oid", "denied-oid",
                  "question", "expected", "labeled-blob", "unlabeled-question", "unlabeled-blob"):
        parser.add_argument(f"--{field}", required=True)
    args = parser.parse_args()
    if not all(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]*", value) for value in (args.account, args.project)):
        parser.error("Use a Foundry account and project name, not a URL")
    if not re.fullmatch(r"[a-z0-9]{3,24}", args.storage_account):
        parser.error("Use a Blob storage account name")
    for oid in (args.allowed_oid, args.denied_oid):
        if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", oid):
            parser.error("Test users must be specified by their Entra object IDs")
    if args.allowed_oid.lower() == args.denied_oid.lower():
        parser.error("Use two different same-tenant test users")
    if args.labeled_blob.casefold() == args.unlabeled_blob.casefold() or not all(
        value.strip() for value in (args.labeled_blob, args.unlabeled_blob, args.expected,
                                    args.question, args.unlabeled_question)
    ):
        parser.error("Use two distinct fixtures and nonempty test questions and expected value")
    if not sys.stdin.isatty():
        parser.error("Run in a terminal; tokens are prompted without echo and are never saved")
    tokens = [getpass(f"{label}: ").strip() for label in (
        "Trusted backend Foundry access token", "Approved user's delegated Search token",
        "Denied user's delegated Search token", "Approved user's expired delegated Search token",
    )]
    if any(not token for token in tokens) or len(set(tokens)) != len(tokens):
        parser.error("Supply four distinct tokens without a Bearer prefix")
    try:
        with httpx.Client(timeout=90, trust_env=False, follow_redirects=False) as http:
            run_tests(http, args, *tokens)
    except (httpx.RequestError, RuntimeError, TypeError, KeyError) as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        raise SystemExit(f"Live Purview acceptance failed: {detail}") from None
    print("Live Purview acceptance passed.")


if __name__ == "__main__":
    main()
