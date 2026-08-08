# ============================================================
# FILE: app.py (fully updated – works with TikTokApi v6+)
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
# Rate limiter
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
# Delays
# -----------------------------------------------------------
current_follow_delay = float(os.environ.get("FOLLOW_DELAY", 4.0))
current_dm_delay = float(os.environ.get("DM_DELAY", 6.0))

# -----------------------------------------------------------
# Tasks
# -----------------------------------------------------------
active_tasks: Dict[str, Dict] = {}

# -----------------------------------------------------------
# Cookie loader
# -----------------------------------------------------------
def load_cookies() -> Optional[Dict[str, Any]]:
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
# Authenticated API session creator
# -----------------------------------------------------------
async def create_authenticated_api() -> TikTokApi:
    """Returns an authenticated TikTokApi instance using cookies."""
    cookies = load_cookies()
    if not cookies:
        raise ValueError("No cookies found. Set TIKTOK_COOKIES_JSON or place cookies.json.")

    ms_token = cookies.get("msToken")
    if not ms_token:
        raise ValueError("No 'msToken' in cookies. Re-export fresh cookies.")

    api = TikTokApi()
    await api.create_sessions(
        ms_tokens=[ms_token],
        num_sessions=1,
        headless=True,
        sleep_after=3
    )
    return api

# -----------------------------------------------------------
# Core helpers
# -----------------------------------------------------------
async def get_followers(api, username: str, limit: int = 100):
    user = api.user(username=username)
    count = 0
    async for follower in user.followers(amount=limit):
        yield follower.as_dict.get("unique_id", "")
        count += 1
        if count >= limit:
            break

async def send_dm(api, target_username: str, message: str):
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
# Background tasks
# -----------------------------------------------------------
async def mass_follow_followers(target_username: str, limit: int, task_id: str = None):
    try:
        api = await create_authenticated_api()
    except Exception as e:
        return False, f"Auth failed: {e}"

    target_username = target_username.lstrip('@')
    success = 0
    skipped = 0
    failed = 0

    try:
        async for follower_name in get_followers(api, target_username, limit):
            if task_id and active_tasks.get(task_id, {}).get("cancel"):
                return True, f"Cancelled. Followed {success}, skipped {skipped}, failed {failed}."

            try:
                follower = api.user(username=follower_name)
                await follower.follow()
                success += 1
                await asyncio.sleep(current_follow_delay)
            except Exception as e:
                if "already following" in str(e).lower():
                    skipped += 1
                else:
                    failed += 1
                    if "ActionBlocked" in str(e):
                        return True, f"Hit a block. Followed {success}, skipped {skipped}, failed {failed}."
        return True, f"Complete. Followed {success}, skipped {skipped}, failed {failed}."
    except Exception as e:
        return False, f"Error: {e}"
    finally:
        if task_id and task_id in active_tasks:
            del active_tasks[task_id]

async def mass_follow_and_dm(target_username: str, dm_message: str, limit: int, task_id: str = None):
    try:
        api = await create_authenticated_api()
    except Exception as e:
        return False, f"Auth failed: {e}"

    target_username = target_username.lstrip('@')
    success_follow = 0
    success_dm = 0
    skip_follow = 0
    fail_follow = 0
    fail_dm = 0

    try:
        async for follower_name in get_followers(api, target_username, limit):
            if task_id and active_tasks.get(task_id, {}).get("cancel"):
                return True, f"Cancelled. Followed {success_follow}, DM'd {success_dm}."

            # Follow
            try:
                follower = api.user(username=follower_name)
                await follower.follow()
                success_follow += 1
                await asyncio.sleep(current_follow_delay)
            except Exception as e:
                if "already following" in str(e).lower():
                    skip_follow += 1
                else:
                    fail_follow += 1
                    if "ActionBlocked" in str(e):
                        return True, f"Blocked. Followed {success_follow}, DM'd {success_dm}."
                    continue

            # DM
            try:
                await send_dm(api, follower_name, dm_message)
                success_dm += 1
                await asyncio.sleep(current_dm_delay)
            except Exception as e:
                fail_dm += 1
                if "ActionBlocked" in str(e):
                    return True, f"Blocked. Followed {success_follow}, DM'd {success_dm}."
                continue

        return True, f"Done. Followed {success_follow}, DM'd {success_dm}, skipped {skip_follow}, follow fails {fail_follow}, DM fails {fail_dm}."
    except Exception as e:
        return False, f"Error: {e}"
    finally:
        if task_id and task_id in active_tasks:
            del active_tasks[task_id]

# -----------------------------------------------------------
# Flask routes (unchanged)
# -----------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def api_status():
    cookies = load_cookies()
    return jsonify({"authenticated": cookies is not None})

@app.route('/api/send', methods=['POST'])
def api_send():
    if not rate_limiter.allow():
        return jsonify({"success": False, "error": "Too many requests."}), 429
    data = request.json or {}
    username = data.get('username', '').strip()
    message = data.get('message', '').strip()
    if not username or not message:
        return jsonify({"success": False, "error": "Username and message required."}), 400
    if len(message) > 500:
        return jsonify({"success": False, "error": "Message too long."}), 400

    cookies = load_cookies()
    if not cookies:
        return jsonify({"success": False, "error": "No session."}), 401

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        api = loop.run_until_complete(create_authenticated_api())
        loop.run_until_complete(send_dm(api, username, message))
        return jsonify({"success": True, "message": f"DM sent to @{username}"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        loop.close()

@app.route('/api/follow', methods=['POST'])
def api_follow():
    if not rate_limiter.allow():
        return jsonify({"success": False, "error": "Too many requests."}), 429
    data = request.json or {}
    username = data.get('username', '').strip()
    if not username:
        return jsonify({"success": False, "error": "Username required."}), 400

    cookies = load_cookies()
    if not cookies:
        return jsonify({"success": False, "error": "No session."}), 401

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        api = loop.run_until_complete(create_authenticated_api())
        user = api.user(username=username)
        loop.run_until_complete(user.follow())
        return jsonify({"success": True, "message": f"Now following @{username}"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        loop.close()

@app.route('/api/mass_follow', methods=['POST'])
def api_mass_follow():
    if not rate_limiter.allow():
        return jsonify({"success": False, "error": "Too many requests."}), 429
    data = request.json or {}
    target = data.get('target_username', '').strip()
    limit = min(int(data.get('limit', 50)), 200)
    if not target:
        return jsonify({"success": False, "error": "Target username required."}), 400

    task_id = str(uuid.uuid4())
    active_tasks[task_id] = {"cancel": False}

    def run_task():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success, msg = loop.run_until_complete(mass_follow_followers(target, limit, task_id))
        active_tasks[task_id]["result"] = {"success": success, "message": msg}
        loop.close()

    threading.Thread(target=run_task).start()
    return jsonify({"success": True, "task_id": task_id, "message": f"Mass follow started for @{target}."})

@app.route('/api/mass_follow_dm', methods=['POST'])
def api_mass_follow_dm():
    if not rate_limiter.allow():
        return jsonify({"success": False, "error": "Too many requests."}), 429
    data = request.json or {}
    target = data.get('target_username', '').strip()
    dm_message = data.get('dm_message', '').strip()
    limit = min(int(data.get('limit', 50)), 200)
    if not target or not dm_message:
        return jsonify({"success": False, "error": "Target and message required."}), 400

    task_id = str(uuid.uuid4())
    active_tasks[task_id] = {"cancel": False}

    def run_task():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success, msg = loop.run_until_complete(mass_follow_and_dm(target, dm_message, limit, task_id))
        active_tasks[task_id]["result"] = {"success": success, "message": msg}
        loop.close()

    threading.Thread(target=run_task).start()
    return jsonify({"success": True, "task_id": task_id, "message": f"Follow+DM loop started for @{target}."})

@app.route('/api/task_status/<task_id>')
def task_status(task_id):
    task = active_tasks.get(task_id)
    if not task:
        return jsonify({"success": False, "error": "Task not found."}), 404
    if "result" in task:
        return jsonify({"success": True, "completed": True, **task["result"]})
    return jsonify({"success": True, "completed": False, "message": "Still running..."})

@app.route('/api/cancel_task/<task_id>', methods=['POST'])
def cancel_task(task_id):
    task = active_tasks.get(task_id)
    if not task:
        return jsonify({"success": False, "error": "Task not found."}), 404
    task["cancel"] = True
    return jsonify({"success": True, "message": "Cancellation requested."})

@app.route('/api/set_delays', methods=['POST'])
def set_delays():
    global current_follow_delay, current_dm_delay
    data = request.json or {}
    new_follow = data.get('follow_delay')
    new_dm = data.get('dm_delay')
    try:
        if new_follow is not None:
            val = float(new_follow)
            if 1.0 <= val <= 30.0:
                current_follow_delay = val
            else:
                return jsonify({"success": False, "error": "Follow delay must be 1-30s."}), 400
        if new_dm is not None:
            val = float(new_dm)
            if 2.0 <= val <= 60.0:
                current_dm_delay = val
            else:
                return jsonify({"success": False, "error": "DM delay must be 2-60s."}), 400
    except ValueError:
        return jsonify({"success": False, "error": "Invalid number."}), 400
    return jsonify({"success": True, "follow_delay": current_follow_delay, "dm_delay": current_dm_delay})

@app.route('/api/get_delays')
def get_delays():
    return jsonify({"follow_delay": current_follow_delay, "dm_delay": current_dm_delay})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=False)
