#!/usr/bin/env python3
"""Resolve a Purview lifecycle principal in the deployment subscription's tenant."""

import argparse
import json
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def graph_principals(subscription, tenant, attribute, value):
    credentials = json.loads(subprocess.check_output([
        "az", "account", "get-access-token", "--subscription", subscription,
        "--resource-type", "ms-graph", "--output", "json",
    ]))
    if credentials.get("tenant", "").lower() != tenant.lower():
        raise ValueError("Microsoft Graph credential does not belong to the deployment tenant")
    path = "me?$select=id"
    if attribute:
        escaped = value.replace("'", "''")
        query = urlencode({"$filter": f"{attribute} eq '{escaped}'", "$select": "id"})
        path = f"servicePrincipals?{query}"
    request = Request(
        f"https://graph.microsoft.com/v1.0/{path}",
        headers={"Authorization": f"Bearer {credentials['accessToken']}"},
    )
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if attribute:
        if result.get("@odata.nextLink"):
            raise ValueError("Microsoft Graph returned more than one page for an exact principal lookup")
        return [principal["id"] for principal in result["value"]]
    return [result["id"]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--tenant", required=True)
    lookup = parser.add_mutually_exclusive_group()
    lookup.add_argument("--app-id")
    lookup.add_argument("--display-name")
    args = parser.parse_args()
    attribute, value = ("appId", args.app_id) if args.app_id else ("displayName", args.display_name)
    try:
        for principal in graph_principals(args.subscription, args.tenant, attribute if value else None, value):
            print(principal)
    except HTTPError as exc:
        raise SystemExit(f"Microsoft Graph principal lookup failed: HTTP {exc.code}") from None
    except (URLError, OSError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        raise SystemExit(f"Microsoft Graph principal lookup failed: {detail}") from None


if __name__ == "__main__":
    main()
