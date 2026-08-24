"""Print the DeepSeek account balance.

The key comes from DEEPSEEK_API_KEY in .env, same as everywhere else. It used
to be a string literal in this file, which is exactly how a live key ends up in
a public repo - there is no such thing as a script too small to leak one.
"""

import sys
from pathlib import Path

import requests

from provider import _env_value

API_KEY = _env_value("DEEPSEEK_API_KEY")
if not API_KEY:
    sys.exit(f"No DEEPSEEK_API_KEY in {Path(__file__).parent.parent / '.env'}")

response = requests.get(
    "https://api.deepseek.com/user/balance",
    headers={
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
    },
    timeout=10,
)

response.raise_for_status()

data = response.json()

print(f"Available: {data['is_available']}")

for balance in data["balance_infos"]:
    print(f"Currency: {balance['currency']}")
    print(f"Total: {balance['total_balance']}")
    print(f"Granted: {balance['granted_balance']}")
    print(f"Topped up: {balance['topped_up_balance']}")
    print()
