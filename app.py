# ============================================================
# FILE: app.py (updated for TikTokApi v6+)
# ============================================================
import json
import asyncio
import os
import threading
import uuid
import time
from pathlib import Path
from typing import Optional, Dict, Any

from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from TikTokApi import TikTokApi
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "default-secret-change-me")
CORS(app)

# -----------------------------------------------------------
# Rate limiter (global, 5 requests per minute)
# -----------------------------------------------------------
class RateLimiter:
    def __init__(self, max_per_minute=5):
        self.max_per_minute = max_per_minute
        self.timestamps = []

    def allow(self) -> bool:
        now = time.time()
        self.timestamps = [t for t in self.timestamps if now - t < 60]
        if len(self.timestamps) >= self.max_per_minute:
            return False
        self.timestamps.append(now)
        return True

rate_limiter = RateLimiter()

# -----------------------------------------------------------
# Delay settings (adjustable via web UI)
# -----------------------------------------------------------
current_follow_delay = float(os.environ.get("FOLLOW_DELAY", 4.0))
current_dm_delay = float(os.environ.get("DM_DELAY", 6.0))

# -----------------------------------------------------------
# Active background tasks
# -----------------------------------------------------------
active_tasks: Dict[str, Dict] = {}

# -----------------------------------------------------------
# Cookie management
# -----------------------------------------------------------
def load_cookies() -> Optional[Dict[str, Any]]:
    """Load TikTok cookies from environment variable or cookies.json file."""
    env_cookies = os.environ.get("TIKTOK_COOKIES_JSON")
    if env_cookies:
        try:
            import base64
            decoded = base64.b64decode(env_cookies).decode()
            return json.loads(decoded)
        except Exception:
            try:
                return json.loads(env_cookies)
            except json.JSONDecodeError:
                pass

    cookie_file = Path("cookies.json")
    if cookie_file.exists():
        with open(cookie_file) as f:
            return json.load(f)

    return None

# -----------------------------------------------------------
# Helper to extract msToken and create API session
# -----------------------------------------------------------
async def create_api_session(cookies: Dict) -> TikTokApi:
    """Create a TikTokApi instance with the msToken from cookies."""
    ms_token = cookies.get("msToken")
    if not ms_token:
        raise ValueError("No 'msToken' found in cookies. Re‑export fresh cookies.")
    
    api = TikTokApi()
    await api.create_sessions(
        ms_tokens=[ms_token],
        num_sessions=1,
        headless=True,
        sleep_after=3
    )
    return api

# -----------------------------------------------------------
# Core functions (using new authentication)
# -----------------------------------------------------------
async def get_followers(api, username: str, limit: int = 100):
    """Async generator yielding follower usernames."""
    user = api.user(username=username)
    count = 0
    async for follower in user.followers(amount=limit):
        yield follower.as_dict.get("unique_id", "")
        count += 1
        if count >= limit:
            break

async def send_dm(api, target_username: str, message: str):
    """Core DM sending logic."""
    target_username = target_username.lstrip('@')
    user = api.user(username=target_username)
    user_info = await user.info()
    if not user_info:
        raise Exception(f"User '@{target_username}' not found or is private.")

    conversations = await api.dm().get_conversations()
    target_user_id = user_info["user"]["id"]
    conversation_id = None
    for conv in conversations:
        if conv.get("conversation_type") == 1 and conv.get("other_user", {}).get("id") == target_user_id:
            conversation_id = conv["conversation_id"]
            break
    if not conversation_id:
        created = await api.dm().create_conversation(user_id=target_user_id)
        conversation_id = created["conversation_id"]

    await api.dm().send_message(conversation_id=conversation_id, text=message)

# -----------------------------------------------------------
# Background tasks (updated)
# -----------------------------------------------------------
async def mass_follow_followers(target_username: str, limit: int, task_id: str = None):
    """Follow followers of target_username."""
    cookies = load_cookies()
    if not cookies:
        return False, "No TikTok session found."
    
    try:
        async with TikTokApi() as api:
            await create_api_session(cookies)  # but we need to pass api? Actually we need to call on the api instance.
            # Let's restructure: we'll create session inside the context.
            # Better: use the helper.
    except Exception as e:
        return False, f"Authentication failed: {e}"
    
    # The above is incomplete - we'll rewrite using a helper that returns api.
    # I'll rewrite the whole function below.
