#!/usr/bin/env python3
"""Evaluate pull request reviews and enforce the HomeLab Review gate.

Uses Python 3 standard library only. Evaluates reviews strictly against the
current PR head commit (`head_sha`) in chronological order (`submitted_at` / `id`),
so stale approvals from earlier commits never satisfy the gate.

Optionally posts a GitHub commit status (`context: "Review gate"`) via the
GitHub REST API using `GITHUB_TOKEN` (never prints or logs tokens).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_MIN_COMMENT_LEN = 40
STATUS_CONTEXT = "Review gate"
GITHUB_API_BASE = "https://api.github.com"

_CHANGES_REQUESTED_LINE_RE = re.compile(
    r"^(?:VERDICT\s*:\s*)?CHANGES[\s_-]+REQUESTED\b", re.IGNORECASE
)
_APPROVED_LINE_RE = re.compile(r"^(?:VERDICT\s*:\s*)?APPROVED\b", re.IGNORECASE)
_LINK_NEXT_RE = re.compile(r'<([^<>]+)>\s*;\s*rel=["\']?next["\']?', re.IGNORECASE)


def _strip_markdown_wrappers(line: str) -> str:
    """Strip markdown heading/list/quote prefixes and inline formatting wrappers."""
    cleaned = line.strip()
    while True:
        prev = cleaned
        cleaned = re.sub(r"^(?:#{1,6}\s*|[-*+]\s+|>\s+)", "", cleaned).strip()
        cleaned = cleaned.strip("*_`~").strip()
        if cleaned == prev:
            break
    cleaned = re.sub(r"\*\*|__|~~|`", "", cleaned)
    cleaned = re.sub(r"(?<!\w)[*_]+|[*_]+(?!\w)", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _first_non_empty_line(body: str) -> str:
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if stripped:
            return stripped
    return ""


def _review_sort_key(indexed_review: tuple[int, dict[str, Any]]) -> tuple[int, str, int, int]:
    idx, review = indexed_review
    submitted_at = str(review.get("submitted_at") or "").strip()
    raw_id = review.get("id")
    review_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else idx
    return (0 if submitted_at else 1, submitted_at, review_id, idx)


def evaluate_reviews(
    reviews: list[dict[str, Any]],
    head_sha: str,
    min_comment_len: int = DEFAULT_MIN_COMMENT_LEN,
) -> tuple[bool, str]:
    """Evaluate PR reviews chronologically against `head_sha`.

    Returns `(approved, description)`:
      - Ignores reviews whose `commit_id != head_sha`.
      - Ignores reviews with state `DISMISSED` or `PENDING`.
      - Latest decision on `head_sha` wins:
          - Native `APPROVED` -> APPROVE
          - Native `CHANGES_REQUESTED` -> BLOCK
          - `COMMENTED`: inspects first non-empty line (stripping markdown):
              - `^CHANGES[\\s_-]+REQUESTED\\b` -> BLOCK
              - `^(VERDICT\\s*:\\s*)?APPROVED\\b` -> APPROVE only if
                `len(body.strip()) >= min_comment_len`
              - Anything else -> neutral
    """
    normalized_sha = head_sha.strip()
    if not normalized_sha:
        raise ValueError("head_sha must be non-empty")

    short_sha = normalized_sha[:7]
    latest_decision: str | None = None
    too_brief_approval_seen = False

    ordered = [
        review
        for _idx, review in sorted(enumerate(reviews), key=_review_sort_key)
    ]

    for review in ordered:
        if not isinstance(review, dict):
            continue

        commit_id = str(review.get("commit_id") or "").strip()
        if commit_id != normalized_sha:
            continue

        state = str(review.get("state") or "").strip().upper()
        if state in {"DISMISSED", "PENDING", ""}:
            continue

        if state == "APPROVED":
            latest_decision = "APPROVE"
            too_brief_approval_seen = False
            continue

        if state == "CHANGES_REQUESTED":
            latest_decision = "BLOCK"
            too_brief_approval_seen = False
            continue

        if state == "COMMENTED":
            body = str(review.get("body") or "")
            body_stripped = body.strip()
            first_line = _strip_markdown_wrappers(_first_non_empty_line(body))
            if not first_line:
                continue

            if _CHANGES_REQUESTED_LINE_RE.match(first_line):
                latest_decision = "BLOCK"
                too_brief_approval_seen = False
            elif _APPROVED_LINE_RE.match(first_line):
                if len(body_stripped) >= min_comment_len:
                    latest_decision = "APPROVE"
                    too_brief_approval_seen = False
                else:
                    too_brief_approval_seen = True

    if latest_decision == "APPROVE":
        return True, f"Full review approved on {short_sha}"
    if latest_decision == "BLOCK":
        return False, f"Changes requested on {short_sha}"
    if too_brief_approval_seen:
        return False, f"Approval comment too brief for full review on {short_sha}"
    return False, f"Awaiting full review approval on {short_sha}"


def _github_request(
    url: str,
    token: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "homelab-review-gate",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return int(resp.status), resp_headers, resp.read()
    except urllib.error.HTTPError as err:
        err_body = err.read().decode("utf-8", errors="replace") if err.fp else ""
        raise RuntimeError(
            f"GitHub API {method} {url} failed with HTTP {err.code}: {err_body[:300]}"
        ) from None


def fetch_pr_reviews(
    repo: str,
    pr_number: int,
    token: str,
    *,
    api_base: str = GITHUB_API_BASE,
) -> list[dict[str, Any]]:
    """Fetch all paginated reviews for a pull request from GitHub REST API."""
    base_url = api_base.rstrip("/")
    encoded_repo = "/".join(
        urllib.parse.quote(part, safe="") for part in repo.strip("/").split("/")
    )
    next_url: str | None = (
        f"{base_url}/repos/{encoded_repo}/pulls/{pr_number}/reviews?per_page=100"
    )
    all_reviews: list[dict[str, Any]] = []

    while next_url:
        _status, headers, body = _github_request(next_url, token)
        page_items = json.loads(body.decode("utf-8"))
        if not isinstance(page_items, list):
            raise RuntimeError(f"Expected JSON list from {next_url}, got {type(page_items).__name__}")
        all_reviews.extend(item for item in page_items if isinstance(item, dict))

        link_header = headers.get("link", "")
        next_match = _LINK_NEXT_RE.search(link_header)
        candidate = next_match.group(1) if next_match else None
        if candidate is not None and not candidate.startswith(f"{base_url}/"):
            raise RuntimeError(f"Refusing unexpected pagination URL outside API base: {candidate}")
        next_url = candidate

    return all_reviews


def post_commit_status(
    repo: str,
    head_sha: str,
    token: str,
    *,
    approved: bool,
    description: str,
    target_url: str | None = None,
    context: str = STATUS_CONTEXT,
    api_base: str = GITHUB_API_BASE,
) -> None:
    """Post a GitHub commit status (`success` or `failure`) for `head_sha`."""
    encoded_repo = "/".join(
        urllib.parse.quote(part, safe="") for part in repo.strip("/").split("/")
    )
    encoded_sha = urllib.parse.quote(head_sha.strip(), safe="")
    url = f"{api_base.rstrip('/')}/repos/{encoded_repo}/statuses/{encoded_sha}"

    payload: dict[str, Any] = {
        "state": "success" if approved else "failure",
        "context": context,
        "description": description[:140],
    }
    if target_url:
        payload["target_url"] = target_url

    _github_request(url, token, method="POST", payload=payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate PR reviews on the current HEAD commit for HomeLab Review gate."
    )
    parser.add_argument(
        "--repo",
        help="GitHub repository in owner/repo format (required for API calls).",
    )
    parser.add_argument(
        "--pr",
        type=int,
        help="Pull request number (required when fetching reviews from GitHub API).",
    )
    parser.add_argument(
        "--head-sha",
        required=True,
        help="Current PR HEAD commit SHA that reviews must match.",
    )
    parser.add_argument(
        "--reviews-json",
        type=Path,
        help="Optional local JSON file of PR review objects (skips fetching from GitHub API).",
    )
    parser.add_argument(
        "--min-comment-len",
        type=int,
        default=DEFAULT_MIN_COMMENT_LEN,
        help=f"Minimum body length for COMMENTED approvals (default: {DEFAULT_MIN_COMMENT_LEN}).",
    )
    parser.add_argument(
        "--target-url",
        default="",
        help="Optional target_url attached when posting the GitHub commit status.",
    )
    parser.add_argument(
        "--post-status",
        action="store_true",
        help="Post the resulting 'Review gate' commit status to GitHub and exit 0.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "").strip()

    if args.reviews_json is not None:
        if not args.reviews_json.is_file():
            print(f"Error: reviews JSON file not found: {args.reviews_json}", file=sys.stderr)
            return 1
        raw_reviews = json.loads(args.reviews_json.read_text(encoding="utf-8"))
        if not isinstance(raw_reviews, list):
            print("Error: --reviews-json must contain a JSON array", file=sys.stderr)
            return 1
        reviews = [item for item in raw_reviews if isinstance(item, dict)]
    else:
        if not args.repo or args.pr is None:
            parser.error("--repo and --pr are required unless --reviews-json is provided")
        if not token:
            print("Error: GITHUB_TOKEN environment variable is required for GitHub API requests", file=sys.stderr)
            return 1
        reviews = fetch_pr_reviews(args.repo, args.pr, token)

    approved, description = evaluate_reviews(
        reviews,
        args.head_sha,
        min_comment_len=args.min_comment_len,
    )
    state_label = "success" if approved else "failure"
    print(f"Review gate [{state_label}]: {description}")

    if args.post_status:
        if not args.repo:
            parser.error("--repo is required with --post-status")
        if not token:
            print("Error: GITHUB_TOKEN environment variable is required for --post-status", file=sys.stderr)
            return 1
        post_commit_status(
            args.repo,
            args.head_sha,
            token,
            approved=approved,
            description=description,
            target_url=args.target_url or None,
        )
        print(f"Posted commit status '{STATUS_CONTEXT}' -> {state_label} on {args.head_sha[:7]}")
        return 0

    return 0 if approved else 1


if __name__ == "__main__":
    raise SystemExit(main())
