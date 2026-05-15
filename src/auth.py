import os
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from logger import get_logger

logger = get_logger()

SCOPES = [
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/contacts.readonly"
]

os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

def get_credentials(account):
    token_file = account["token_file"]
    credentials_file = account["credentials_file"]
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds, token_file)
            logger.info(f"Token refreshed for {account['email']}")
        except Exception as e:
            logger.error(f"Token refresh failed for {account['email']}: {e}")
            creds = None
    if not creds or not creds.valid:
        flow = Flow.from_client_secrets_file(
            credentials_file,
            scopes=SCOPES,
            redirect_uri=config.get("oauth_redirect_uri", "http://localhost:8085")
        )
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent"
        )
        print("\n")
        print("=" * 60)
        print("GOOGLE AUTHORIZATION REQUIRED")
        print("=" * 60)
        print(f"Account: {account['email']}")
        print("\nOpen this URL in your browser:\n")
        print(auth_url)
        print("\n" + "=" * 60)
        print("After approving, copy the FULL URL from your browser address bar")
        print("and paste it here:")
        redirected_url = input("Paste the full redirect URL here: ").strip()
        os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
        flow.fetch_token(authorization_response=redirected_url)
        creds = flow.credentials
        _save_token(creds, token_file)
        logger.info(f"Token saved for {account['email']}")
    return creds

def _save_token(creds, token_file):
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    with open(token_file, "w") as f:
        f.write(creds.to_json())
