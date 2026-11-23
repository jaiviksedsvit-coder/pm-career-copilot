"""
auth_gmail_server.py — Background OAuth Server with Multi-Account Support
==========================================================================
Starts local OAuth redirect server on localhost:8080 (or dynamic port),
writes the exact authorization URL to oauth_url.txt,
and waits for the browser redirect callback to save token_primary.json or token_secondary.json.

Usage:
  python auth_gmail_server.py --account primary     # jaivik.chauhan.2026@gmail.com
  python auth_gmail_server.py --account secondary   # jaivik.sedsvit@gmail.com
"""

import sys
import argparse
import wsgiref.simple_server
from pathlib import Path

# Local project imports
ROOT = Path(__file__).parent
from outreach_drafter import (
    get_credentials_path,
    GMAIL_PRIMARY_SCOPES,
    GMAIL_SECONDARY_SCOPES,
    TOKEN_PRIMARY_FILE,
    TOKEN_SECONDARY_FILE
)
from google_auth_oauthlib.flow import (
    InstalledAppFlow,
    _RedirectWSGIApp,
    _ExclusiveWSGIServer,
    _WSGIRequestHandler
)

def start_auth_server(account: str = "primary"):
    cred_path = get_credentials_path()
    if not cred_path:
        print("ERROR: No credentials.json or client_secret*.json found.", flush=True)
        sys.exit(1)

    if account == "secondary":
        target_token_file = TOKEN_SECONDARY_FILE
        scopes = GMAIL_SECONDARY_SCOPES
        account_name = "Secondary (jaivik.sedsvit@gmail.com)"
    else:
        target_token_file = TOKEN_PRIMARY_FILE
        scopes = GMAIL_PRIMARY_SCOPES
        account_name = "Primary (jaivik.chauhan.2026@gmail.com)"

    flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), scopes)

    success_msg = f"""
    <html>
      <head><title>PM Career Copilot - Authorized</title></head>
      <body style="font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; height: 90vh; background: #0f172a; color: #f8fafc;">
        <div style="background: #1e293b; padding: 40px; border-radius: 16px; text-align: center; max-width: 500px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); border: 1px solid #334155;">
          <h1 style="color: #10b981; font-size: 28px; margin-bottom: 12px;">✅ Authorization Successful!</h1>
          <p style="font-size: 16px; color: #94a3b8; line-height: 1.5;">
            Connected {account_name} for PM Career Copilot.<br>
            Token saved to <code>{target_token_file.name}</code>.<br><br>
            You may safely close this tab and return to chat.
          </p>
        </div>
      </body>
    </html>
    """

    wsgi_app = _RedirectWSGIApp(success_msg)

    # Attempt to bind to standard localhost:8080, fallback to port 0 if in use
    local_server = None
    for target_port in [8080, 0]:
        try:
            local_server = wsgiref.simple_server.make_server(
                "localhost",
                target_port,
                wsgi_app,
                server_class=_ExclusiveWSGIServer,
                handler_class=_WSGIRequestHandler,
            )
            break
        except Exception:
            continue

    if not local_server:
        print("ERROR: Could not bind local HTTP server to any port.", flush=True)
        sys.exit(1)

    allocated_port = local_server.server_port
    flow.redirect_uri = f"http://localhost:{allocated_port}/"
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

    # Write auth URL to file for instant pickup
    url_file = ROOT / "oauth_url.txt"
    url_file.write_text(auth_url, encoding="utf-8")
    print(f"ACCOUNT: {account.upper()} -> {target_token_file.name}", flush=True)
    print(f"OAUTH_URL_FILE_WRITTEN: {allocated_port}", flush=True)
    print(f"AUTH_URL: {auth_url}", flush=True)

    try:
        # Wait for Google to redirect back to http://localhost:<port>/
        local_server.handle_request()

        auth_response = wsgi_app.last_request_uri.replace("http://", "https://")
        flow.fetch_token(authorization_response=auth_response)

        with open(target_token_file, "w", encoding="utf-8") as f:
            f.write(flow.credentials.to_json())

        print(f"TOKEN_SAVED_SUCCESSFULLY: {target_token_file.name}", flush=True)
    finally:
        local_server.server_close()
        if url_file.exists():
            try:
                url_file.unlink()
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gmail OAuth Server for PM Career Copilot")
    parser.add_argument("--account", choices=["primary", "secondary"], default="primary", help="Which account to authorize")
    args = parser.parse_args()
    start_auth_server(account=args.account)
