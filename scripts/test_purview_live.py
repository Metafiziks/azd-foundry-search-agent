#!/usr/bin/env python3
"""Verify protected ingestion and retrieval through a deployed Foundry Responses endpoint."""

import argparse
import json
import re
import subprocess
import sys
import time
from getpass import getpass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote

import httpx

from ingest_purview import corpus_files, ingest, validate_ingest_args

NO_DOCUMENTS = "I could not find information about that in the available documents."
CASE_FIELDS = ("allowed_oid", "denied_oid", "question", "expected", "labeled_blob", "unlabeled_question", "unlabeled_blob")


def load_config(path):
    with path.open(encoding="utf-8") as file:
        config = json.load(file)
    fields = (*CASE_FIELDS, "directory", "unlabeled_expected")
    if not isinstance(config, dict) or set(config) != set(fields) or not all(
        isinstance(value, str) and value.strip() for value in config.values()
    ):
        raise ValueError(f"Acceptance JSON must contain exactly these nonempty strings: {', '.join(fields)}")
    if config["unlabeled_expected"].casefold() == config["expected"].casefold():
        raise ValueError("The labeled and unlabeled test facts must be different")
    return config


def acceptance_files(args):
    root, files = corpus_files(args.directory)
    if {path.relative_to(root).as_posix() for path in files} != {args.labeled_blob, args.unlabeled_blob}:
        raise ValueError("Use a dedicated directory containing exactly the two named synthetic fixtures")
    return root, files


def read_token(label):
    prompt = f"{label}: "
    try:
        import termios
    except ImportError:
        return getpass(prompt).strip()

    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    mode = termios.tcgetattr(fd)
    # macOS canonical input cannot hold a full Entra access token.
    mode[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHONL)
    mode[6][termios.VMIN] = 1
    mode[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSAFLUSH, mode)
    try:
        return getpass(prompt).strip()
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, original)


def response_text(body):
    return "\n".join(
        part["text"]
        for item in body.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )


def run_tests(http, args, backend_token, allowed_token, denied_token, expired_token=None):
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

    status, body = invoke("denied", args.denied_oid, denied_token, args.question)
    if status != 200 or body.get("status") != "completed" or response_text(body).strip() != NO_DOCUMENTS:
        raise RuntimeError("denied: expected a completed response with no available documents")

    status, body = invoke("unlabeled", args.allowed_oid, allowed_token, args.unlabeled_question)
    text = response_text(body).strip()
    if status != 200 or body.get("status") != "completed" or not text:
        raise RuntimeError("unlabeled: expected a completed response")
    if args.unlabeled_expected:
        if any(value.casefold() in text.casefold() for value in (
            args.unlabeled_expected, args.unlabeled_blob.split("/")[-1],
        )):
            raise RuntimeError("unlabeled: the response disclosed the control fact or cited the control file")
    elif text != NO_DOCUMENTS:
        raise RuntimeError("unlabeled: expected a response with no available documents")

    status, body = invoke("missing token", args.allowed_oid, None, args.question)
    if status not in (401, 403) or response_text(body):
        raise RuntimeError("Missing delegation was not rejected")
    if expired_token is not None:
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
    for field in ("account", "project", "storage-account"):
        parser.add_argument(f"--{field}", required=True)
    for field in (*CASE_FIELDS, "unlabeled_expected"):
        parser.add_argument(f"--{field.replace('_', '-')}")
    parser.add_argument("--config", type=Path, help="Upload and test exactly two fixtures from a local acceptance JSON")
    parser.add_argument("--subscription")
    parser.add_argument("--search-service")
    parser.add_argument("--index")
    args = parser.parse_args()
    if args.config is not None:
        if any(getattr(args, field) is not None for field in (*CASE_FIELDS, "unlabeled_expected")):
            parser.error("Pass acceptance cases in JSON or as command-line options, not both")
        try:
            vars(args).update(load_config(args.config))
            root, files = acceptance_files(args)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        args.timeout = 600
        args.show_indexer_error = True
        validate_ingest_args(parser, args)
    elif any(getattr(args, field) is None for field in CASE_FIELDS):
        parser.error("All acceptance case options are required without --config")
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
    labels = [
        "Trusted backend Foundry access token", "Approved user's delegated Search token",
        "Denied user's delegated Search token",
    ]
    if args.config is None:
        labels.append("Approved user's expired delegated Search token")
    else:
        try:
            ingest(args, root, files)
        except HTTPError as exc:
            raise SystemExit(f"Live Purview ingestion failed: HTTP {exc.code}") from None
        except URLError:
            raise SystemExit("Live Purview ingestion could not reach Foundry IQ") from None
        except subprocess.CalledProcessError:
            raise SystemExit("Azure CLI failed during live Purview ingestion") from None
        except (OSError, ValueError, RuntimeError) as exc:
            raise SystemExit(f"Live Purview ingestion failed: {exc}") from None
    tokens = [read_token(label) for label in labels]
    if any(not token or any(char.isspace() for char in token) for token in tokens) or len(set(tokens)) != len(tokens):
        parser.error("Supply distinct access tokens without a Bearer prefix")
    try:
        with httpx.Client(timeout=90, trust_env=False, follow_redirects=False) as http:
            run_tests(http, args, *tokens)
    except (httpx.RequestError, RuntimeError, TypeError, KeyError) as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        raise SystemExit(f"Live Purview acceptance failed: {detail}") from None
    print("Live Purview acceptance passed.")


if __name__ == "__main__":
    main()
