import os
import json
from telethon import TelegramClient
import config
from error_handler import log_exception

# اطمینان از وجود پوشه سشن‌ها
if not os.path.exists(config.SESSION_DIR):
    os.makedirs(config.SESSION_DIR)


# =========================================================
# 1) SESSION MANAGEMENT + METADATA
# =========================================================

def _meta_path(session_name):
    return os.path.join(config.SESSION_DIR, f"{session_name}.meta.json")


def _load_meta(session_name):
    try:
        path = _meta_path(session_name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}
    except Exception as e:
        log_exception(e, "Error loading session metadata")
        return {}


def _save_meta(session_name, meta: dict):
    try:
        path = _meta_path(session_name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_exception(e, "Error saving session metadata")


def get_active_accounts():
    """جستجو در پوشه سشن‌ها و بازگرداندن لیست نام اکانت‌های فعال + metadata"""
    try:
        files = os.listdir(config.SESSION_DIR)

        accounts = []

        for f in files:
            if f.endswith(".session"):
                name = os.path.splitext(f)[0]

                meta = _load_meta(name)

                accounts.append({
                    "session": name,
                    "meta": meta
                })

        return accounts

    except Exception as e:
        log_exception(e, "Error while listing active sessions")
        return []


def get_available_accounts():
    """نام مستعار برای هماهنگی با خطای فایل ui.py"""
    return get_active_accounts()


# =========================================================
# 2) CLIENT MANAGER (REPLACED create_client)
# =========================================================

_client_cache = {}


def create_client(account_name):
    """
    Client Manager with caching (supports both dict and string input)
    """
    try:
        # normalize input
        if isinstance(account_name, dict):
            session_name = account_name.get("session")
        else:
            session_name = account_name

        if not session_name:
            raise ValueError("session_name is empty or invalid")

        # cache check
        if session_name in _client_cache:
            return _client_cache[session_name]

        session_path = os.path.join(config.SESSION_DIR, session_name)

        client = TelegramClient(
            session_path,
            config.API_ID,
            config.API_HASH
        )

        _client_cache[session_name] = client
        return client

    except Exception as e:
        log_exception(e, "Error creating Telegram client")
        raise


# =========================================================
# REGISTER ACCOUNT (UNCHANGED LOGIC)
# =========================================================

async def register_new_account(session_name):
    """ثبت‌نام و ورود یک اکانت جدید در پوشه سشن‌ها"""
    client = create_client(session_name)

    try:
        print(f"\n[🔄] Connecting to Telegram for session: {session_name}...")
        await client.connect()

        if not await client.is_user_authorized():

            phone = input("Enter Phone Number (e.g., +989123456789): ")
            await client.send_code_request(phone)

            code = input("Enter the verification code you received: ")

            try:
                await client.sign_in(phone, code)
            except Exception:
                two_step_pass = input("Two-step verification enabled. Enter password: ")
                await client.sign_in(password=two_step_pass)

        print(f"✅ Account '{session_name}' successfully authorized!")

        # save metadata
        _save_meta(session_name, {
            "status": "active"
        })

        return True

    except Exception as e:
        log_exception(e, f"Failed to register new account: {session_name}")
        print(f"❌ Error during registration. Check {config.ERROR_LOG_FILE}")
        return False

    finally:
        await client.disconnect()