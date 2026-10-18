"""
setup_tokens.py
===============
Run once per Gmail to obtain an OAuth2 token for YouTube Analytics API.

Usage:
    python setup_tokens.py --gmail 1   # Opens browser, sign in with Gmail 1
    python setup_tokens.py --gmail 2   # Gmail 2 (owns channels 3-4)

Produces: tokens/gmail_<N>_token.json
"""

import os
import sys
import argparse

from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

CLIENT_SECRET_FILE = 'client_secret.json'
TOKEN_DIR = 'tokens'
SCOPES = [
    'https://www.googleapis.com/auth/yt-analytics.readonly',
    'https://www.googleapis.com/auth/youtube.readonly'
]


def setup_token(gmail_index: int):
    os.makedirs(TOKEN_DIR, exist_ok=True)
    token_path = os.path.join(TOKEN_DIR, f'gmail_{gmail_index}_token.json')

    if not os.path.exists(CLIENT_SECRET_FILE):
        print(f"[ERROR] {CLIENT_SECRET_FILE} not found!")
        print("[INFO]  Download it from Google Cloud Console → APIs & Services → Credentials")
        return

    ch_a = gmail_index * 2 - 1
    ch_b = gmail_index * 2
    print(f"[AUTH] Setting up token for Gmail {gmail_index}...")
    print(f"[AUTH] Browser will open — sign in with the Gmail account that owns channels {ch_a} and {ch_b}")

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(token_path, 'w') as f:
        f.write(creds.to_json())

    print(f"[AUTH] ✓ Token saved: {token_path}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Set up OAuth2 token for a Gmail account')
    parser.add_argument('--gmail', type=int, required=True, help='Gmail index (1-10)')
    args = parser.parse_args()
    setup_token(args.gmail)
