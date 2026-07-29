"""Google OAuth for Search Console.

**This module is the one documented exception to R1** (spec §14). It may send,
to Google's OAuth token endpoint only, and it returns a credential rather than
vendor data. Every Search Console *data* call still goes through ``fetch`` and is
cached in ``raw_response`` like any other source.

Tokens are written to ``Paths.state_dir`` at mode 0600. They are deliberately
kept out of the database: ``raw_response`` is append-only and never pruned, so a
credential written there could never be deleted.
"""

from __future__ import annotations

import json
import secrets
import threading
import urllib.parse
import webbrowser
from collections.abc import Mapping
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Final

import httpx

from zipf.clock import from_iso, now, to_iso
from zipf.config import Paths, load_settings
from zipf.errors import CredentialMissingError, VendorError

AUTH_ENDPOINT: Final = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT: Final = "https://oauth2.googleapis.com/token"
SCOPE: Final = "https://www.googleapis.com/auth/webmasters.readonly"
CAPABILITY: Final = "gsc.search_analytics"

#: Refresh slightly before expiry so a long request cannot straddle the boundary.
EXPIRY_MARGIN: Final = timedelta(minutes=2)

#: Bound on the interactive consent step. Without it, a browser the user never
#: completes leaves the CLI blocked forever.
CONSENT_TIMEOUT_S: Final = 300.0


def _make_callback_handler(captured: dict[str, str]) -> type[BaseHTTPRequestHandler]:
    """Build a handler that writes the redirect's query into ``captured``.

    The result dictionary is owned by the caller rather than stored on the class.
    Class-level mutable state would be shared across every authorisation in the
    process, and would let a stale value from an earlier attempt be read as the
    current one.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            for key in ("code", "state", "error"):
                values = query.get(key)
                if values:
                    captured[key] = values[0]

            body = b"<html><body><h3>zipf: authorised. Close this tab.</h3></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            """Silence the default stderr access log."""

    return Handler


def _token_path() -> Path:
    return Paths.resolve().state_dir / "google_token.json"


def _read_token() -> dict[str, Any] | None:
    path = _token_path()
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _write_token(token: Mapping[str, Any]) -> None:
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(token), indent=2), encoding="utf-8")
    path.chmod(0o600)


def _client_credentials() -> tuple[str, str]:
    settings = load_settings()
    if not settings.gsc_client_id:
        raise CredentialMissingError(capability=CAPABILITY, variable="GSC_CLIENT_ID")
    if not settings.gsc_client_secret:
        raise CredentialMissingError(capability=CAPABILITY, variable="GSC_CLIENT_SECRET")
    return settings.gsc_client_id, settings.gsc_client_secret


def _post_token(payload: dict[str, str]) -> dict[str, Any]:
    """Exchange or refresh a token. Synchronous, and only ever hits Google."""
    try:
        response = httpx.post(TOKEN_ENDPOINT, data=payload, timeout=30.0)
    except httpx.HTTPError as exc:
        raise VendorError(capability=CAPABILITY, detail=f"token endpoint: {exc}") from exc

    if response.status_code >= 400:
        raise VendorError(
            capability=CAPABILITY,
            detail=f"token endpoint rejected the request: {response.text[:200]}",
            status=response.status_code,
        )

    token: dict[str, Any] = response.json()
    token["obtained_at"] = to_iso(now())
    return token


def authorise() -> dict[str, Any]:
    """Run the interactive consent flow once and store the resulting token."""
    client_id, client_secret = _client_credentials()

    captured: dict[str, str] = {}
    server = HTTPServer(("127.0.0.1", 0), _make_callback_handler(captured))
    redirect_uri = f"http://127.0.0.1:{server.server_port}"
    state = secrets.token_urlsafe(16)

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # force a refresh_token even on re-authorisation
        "state": state,
    }
    url = f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"

    server.timeout = CONSENT_TIMEOUT_S
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print(f"Opening a browser to authorise Search Console access.\nIf it does not open: {url}")
    webbrowser.open(url)
    try:
        thread.join(timeout=CONSENT_TIMEOUT_S)
    finally:
        server.server_close()

    if "error" in captured:
        raise VendorError(capability=CAPABILITY, detail=f"consent denied: {captured['error']}")

    code = captured.get("code")
    if code is None:
        raise VendorError(
            capability=CAPABILITY,
            detail=f"no authorisation code received within {CONSENT_TIMEOUT_S:.0f}s",
        )
    if not secrets.compare_digest(captured.get("state", ""), state):
        raise VendorError(capability=CAPABILITY, detail="state mismatch on OAuth callback")

    token = _post_token(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    )
    _write_token(token)
    return token


def _is_expired(token: Mapping[str, Any]) -> bool:
    obtained = from_iso(str(token["obtained_at"]))
    lifetime = timedelta(seconds=int(token.get("expires_in", 3600)))
    return now() >= obtained + lifetime - EXPIRY_MARGIN


def _refresh(token: Mapping[str, Any]) -> dict[str, Any]:
    client_id, client_secret = _client_credentials()
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        raise VendorError(
            capability=CAPABILITY,
            detail="stored token has no refresh_token; run `zipf gsc auth` again",
        )

    refreshed = _post_token(
        {
            "refresh_token": str(refresh_token),
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        }
    )
    # A refresh response omits refresh_token; carry the existing one forward or
    # the next refresh has nothing to present.
    refreshed.setdefault("refresh_token", refresh_token)
    _write_token(refreshed)
    return refreshed


def access_token() -> str:
    """Return a valid access token, refreshing or authorising as needed."""
    token = _read_token()
    if token is None:
        token = authorise()
    elif _is_expired(token):
        token = _refresh(token)

    value = token.get("access_token")
    if not isinstance(value, str) or not value:
        raise VendorError(capability=CAPABILITY, detail="token response had no access_token")
    return value


async def auth_headers() -> Mapping[str, str]:
    """Capability auth provider. Returns the bearer header for a GSC request."""
    return {"Authorization": f"Bearer {access_token()}"}
