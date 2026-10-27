"""One-time Google OAuth. Run from the project folder, once per Google account:

    deploy/linux/run brain python -m app.google_auth            # Linux; primary account
    deploy/linux/run brain python -m app.google_auth college    # a second one, labelled
    docker compose run --rm -p 8765:8765 brain python -m app.google_auth   # laptop, Docker

Tokens are stored per label (token.json for primary, token-<label>.json for others). Calendar
uses the primary account; Gmail polls every account that has a token.

It prints a URL; open it in any browser on the laptop, approve, and the browser is redirected to
localhost:8765, which Docker forwards into this container (on the Linux machine, an
`ssh -L 8765:localhost:8765` tunnel from the laptop does it; see deploy/linux/README.md). The
refresh token lands in
GOOGLE_DIR/token.json and the brain refreshes it silently from then on.

Scopes are deliberately narrow: calendar read/write on your calendars, Gmail read + drafts (no send),
Drive read-only. Extra scopes later mean re-running this once.
"""
from __future__ import annotations

import os
import sys

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import settings

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/drive.readonly",
]

CREDENTIALS = os.path.join(settings.google_dir, "credentials.json")


def token_path(label: str = "primary") -> str:
    name = "token.json" if label in ("", "primary") else f"token-{label}.json"
    return os.path.join(settings.google_dir, name)


def accounts() -> list[str]:
    """Labels of every authorised account (primary first)."""
    if not os.path.isdir(settings.google_dir):
        return []
    out = []
    for f in sorted(os.listdir(settings.google_dir)):
        if f == "token.json":
            out.insert(0, "primary")
        elif f.startswith("token-") and f.endswith(".json"):
            out.append(f[len("token-"):-len(".json")])
    return out


def load_credentials(label: str = "primary") -> Credentials | None:
    """Used by the brain at runtime. Refreshes if expired; None if never authorised."""
    path = token_path(label)
    if not os.path.exists(path):
        return None
    creds = Credentials.from_authorized_user_file(path, SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request

        creds.refresh(Request())
        with open(path, "w") as f:
            f.write(creds.to_json())
        return creds
    return None


def main() -> int:
    label = sys.argv[1] if len(sys.argv) > 1 else "primary"
    if not os.path.exists(CREDENTIALS):
        print(f"missing {CREDENTIALS} — download the OAuth desktop client JSON from Google Cloud and put it there")
        return 1
    port = int(os.environ.get("GOOGLE_AUTH_PORT", "8765"))
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
    print(f"\nAuthorising account '{label}'. Open this URL in a browser on the laptop, pick THAT account, "
          "approve, and wait for the redirect:\n")
    creds = flow.run_local_server(host="localhost", bind_addr="0.0.0.0", port=port, open_browser=False,
                                  prompt="consent", access_type="offline")
    os.makedirs(settings.google_dir, exist_ok=True)
    path = token_path(label)
    with open(path, "w") as f:
        f.write(creds.to_json())
    print(f"\nsaved {path}. Restart the brain: systemctl --user restart assistant-brain "
          "(Docker: docker compose restart brain)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
