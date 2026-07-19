"""Linear — optional issue tracking. Off unless linear_api_key + linear_team_id are set.

Nothing in the core loop depends on this module. It exists so someone who
already lives in Linear can opt into progress logging there, without every
user being forced to have a Linear account just to try Supersonic.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from supersonic.config import UserSecrets

logger = logging.getLogger(__name__)

_API_URL = "https://api.linear.app/graphql"


def is_configured(secrets: UserSecrets) -> bool:
    return bool(secrets.linear_api_key.strip() and secrets.linear_team_id.strip())


def create_issue(secrets: UserSecrets, title: str, description: str = "") -> Optional[str]:
    if not is_configured(secrets):
        return None
    query = "mutation IssueCreate($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { url } } }"
    variables = {"input": {"teamId": secrets.linear_team_id, "title": title[:255], "description": description[:5000]}}
    try:
        resp = httpx.post(
            _API_URL,
            json={"query": query, "variables": variables},
            headers={"Authorization": secrets.linear_api_key, "Content-Type": "application/json"},
            timeout=20.0,
        )
        resp.raise_for_status()
        issue = resp.json().get("data", {}).get("issueCreate", {}).get("issue")
        return issue.get("url") if issue else None
    except httpx.HTTPError:
        logger.exception("Linear issue creation failed")
        return None
