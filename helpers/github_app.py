"""
GitHub App authentication helpers.

Generates short-lived App JWTs and exchanges them for installation access
tokens (scoped to one installation, valid 1 hour). Tokens are cached and
refreshed automatically when within 5 minutes of expiry.
"""

import os
import time
from datetime import datetime

import httpx
import jwt
from temporalio.exceptions import ApplicationError

_token_cache: dict[int, tuple[str, float]] = {}  # installation_id → (token, unix_expires_at)


def _load_private_key() -> str:
    """Load the App private key from a file path or inline env var."""
    if path := os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH"):
        path = path.strip()
        if not path or path.startswith("#"):
            pass  # placeholder value from .env template — treat as unset
        else:
            try:
                with open(path) as f:
                    return f.read()
            except FileNotFoundError:
                raise ValueError(
                    f"GITHUB_APP_PRIVATE_KEY_PATH is set to {path!r} but that file does not exist. "
                    "Check the path in your .env file."
                )
    if key := os.environ.get("GITHUB_APP_PRIVATE_KEY"):
        # Deployment platforms often encode newlines as literal \n
        return key.replace("\\n", "\n")
    raise ValueError(
        "GitHub App private key not configured. "
        "Set GITHUB_APP_PRIVATE_KEY_PATH or GITHUB_APP_PRIVATE_KEY."
    )


def _generate_app_jwt() -> str:
    app_id = os.environ["GITHUB_APP_ID"]
    private_key = _load_private_key()
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": app_id}
    return jwt.encode(payload, private_key, algorithm="RS256")


async def get_installation_token(installation_id: int) -> str:
    """Return a valid installation access token, fetching a new one if needed."""
    token, expires_at = _token_cache.get(installation_id, ("", 0.0))
    if token and time.time() < expires_at - 300:  # refresh 5 min before expiry
        return token

    app_jwt = _generate_app_jwt()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    if resp.status_code == 401:
        _token_cache.pop(installation_id, None)  # evict stale entry so next call retries
        raise ApplicationError(
            "GitHub App JWT rejected — check GITHUB_APP_ID and private key",
            non_retryable=True,
        )
    if resp.status_code == 404:
        raise ApplicationError(
            f"Installation {installation_id} not found — App may not be installed on this repo",
            non_retryable=True,
        )
    resp.raise_for_status()

    data = resp.json()
    new_token: str = data["token"]
    exp = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
    _token_cache[installation_id] = (new_token, exp)
    return new_token


async def get_installation_token_for_repo(repo: str) -> str:
    """Resolve the App's installation on ``repo`` ('owner/name') and return an installation token.

    Lets a caller that only knows the repo (e.g. ``scout remediate``) use App auth without first
    having to look up the installation id — it asks GitHub which installation covers the repo,
    then mints a token for it."""
    app_jwt = _generate_app_jwt()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{repo}/installation",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    if resp.status_code == 404:
        raise ApplicationError(
            f"The GitHub App isn't installed on {repo} (or has no access to it). Install it on "
            "the repo and try again.",
            non_retryable=True,
        )
    resp.raise_for_status()
    return await get_installation_token(int(resp.json()["id"]))


async def get_app_bot_identity(installation_token: str) -> tuple[str, str]:
    """Return (name, email) for authoring git commits as the App's bot account, so commits are
    attributed to e.g. 'dependency-scout[bot]' — a real account that shows the bot as author and
    satisfies CLA checks — rather than a made-up user. Email uses GitHub's noreply form
    '<bot-user-id>+<login>@users.noreply.github.com'.

    The App JWT is only valid for /app endpoints, so the bot slug comes from `GET /app` (JWT) but
    the user-id lookup uses the installation token."""
    from urllib.parse import quote

    json_headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    app_jwt = _generate_app_jwt()
    async with httpx.AsyncClient(timeout=15.0) as client:
        app_resp = await client.get(
            "https://api.github.com/app",
            headers={"Authorization": f"Bearer {app_jwt}", **json_headers},
        )
        app_resp.raise_for_status()
        login = f"{app_resp.json()['slug']}[bot]"
        user_resp = await client.get(
            f"https://api.github.com/users/{quote(login)}",
            headers={"Authorization": f"Bearer {installation_token}", **json_headers},
        )
        user_resp.raise_for_status()
        uid = user_resp.json()["id"]
    return login, f"{uid}+{login}@users.noreply.github.com"
