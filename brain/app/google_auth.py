"""One-time Google OAuth. Run from the project folder:

    docker compose run --rm -p 8765:8765 brain python -m app.google_auth

It prints a URL; open it in any browser on the laptop, approve, and the browser is redirected to
localhost:8765, which Docker forwards into this container. The refresh token lands in
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
TOKEN = os.path.join(settings.google_dir, "token.json")


def load_credentials() -> Credentials | None:
    """Used by the brain at runtime. Refreshes if expired; None if never authorised."""
    if not os.path.exists(TOKEN):
        return None
    creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request

        creds.refresh(Request())
        with open(TOKEN, "w") as f:
            f.write(creds.to_json())
        return creds
    return None


def main() -> int:
    if not os.path.exists(CREDENTIALS):
        print(f"missing {CREDENTIALS} — download the OAuth desktop client JSON from Google Cloud and put it there")
        return 1
    port = int(os.environ.get("GOOGLE_AUTH_PORT", "8765"))
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS, SCOPES)
    print("\nOpen this URL in a browser on the laptop, approve, and wait for the redirect:\n")
    creds = flow.run_local_server(host="localhost", bind_addr="0.0.0.0", port=port, open_browser=False,
                                  prompt="consent", access_type="offline")
    os.makedirs(settings.google_dir, exist_ok=True)
    with open(TOKEN, "w") as f:
        f.write(creds.to_json())
    print(f"\nsaved {TOKEN}. Restart the brain: docker compose restart brain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
