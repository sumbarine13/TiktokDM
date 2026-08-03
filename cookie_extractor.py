# ============================================================
# FILE: cookie_extractor.py (run on your local machine)
# ============================================================
"""
TikTok cookie extractor using mitmproxy.
1. Install mitmproxy: pip install mitmproxy
2. Run: mitmdump -s cookie_extractor.py
3. Configure phone/emulator to use proxy on port 8080.
4. Open TikTok, log in. Cookies are saved to cookies.json.
"""

import json
from mitmproxy import http, ctx

captured = {}

def request(flow: http.HTTPFlow):
    if "tiktok.com" in flow.request.pretty_host:
        for name, value in flow.request.cookies.items():
            captured[name] = value

def done():
    if captured:
        with open("cookies.json", "w") as f:
            json.dump(captured, f, indent=2)
        print(f"[AXIOGRAM] Saved {len(captured)} cookies.")
    else:
        print("[AXIOGRAM] No cookies captured. Did you visit TikTok?")
