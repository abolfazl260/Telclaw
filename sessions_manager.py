import asyncio
import json
import os

from telethon import TelegramClient, errors

import config
from error_handler import log_exception

# Ensure the session directory exists.
os.makedirs(config.SESSION_DIR, exist_ok=True)


def _meta_path(session_name):
    return os.path.join(config.SESSION_DIR, f"{session_name}.meta.json")


def _session_path(session_name):
    return os.path.join(config.SESSION_DIR, session_name)


def _normalize_session_name(account):
    """Return a valid session name from a string or account record."""
    if isinstance(account, dict):
        account = account.get("session")

    if not isinstance(account, str):
        raise TypeError(
            f"session_name must be a string, got {type(account).__name__}"
        )

    session_name = account.strip()
    if not session_name:
        raise ValueError("session_name is empty")

    return session_name


def _load_meta(session_name):
    try:
        path = _meta_path(session_name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}
    except Exception as exc:
        log_exception(exc, "Error loading session metadata")
        return {}


def _save_meta(session_name, meta: dict):
    try:
        with open(_meta_path(session_name), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        log_exception(exc, "Error saving session metadata")


def get_active_accounts():
    """Return existing Telegram sessions and their metadata."""
    try:
        accounts = []
        for filename in os.listdir(config.SESSION_DIR):
            if filename.endswith(".session"):
                name = os.path.splitext(filename)[0]
                accounts.append({"session": name, "meta": _load_meta(name)})
        return accounts
    except Exception as exc:
        log_exception(exc, "Error while listing active sessions")
        return []


def get_available_accounts():
    """Backward-compatible alias for the account list."""
    return get_active_accounts()


_client_cache = {}


def create_client(account_name):
    """Create or reuse a Telethon client using a scalar session-name key."""
    try:
        session_name = _normalize_session_name(account_name)

        if session_name in _client_cache:
            return _client_cache[session_name]

        client = TelegramClient(
            _session_path(session_name),
            config.API_ID,
            config.API_HASH,
        )
        _client_cache[session_name] = client
        return client
    except Exception as exc:
        log_exception(exc, "Error creating Telegram client")
        raise


async def _prompt(prompt):
    """Read console input without blocking the asyncio event loop."""
    return (await asyncio.to_thread(input, prompt)).strip()


async def register_new_account(session_name):
    """Create a genuinely new Telegram session and complete interactive login."""
    session_name = _normalize_session_name(session_name)

    # A 'new account' must never silently reuse an existing session.
    if os.path.exists(f"{_session_path(session_name)}.session"):
        print(
            f"❌ Session '{session_name}' already exists. "
            "Choose a different session name or select the existing account."
        )
        return False

    client = create_client(session_name)

    try:
        print(f"\n[🔄] Connecting to Telegram for new session: {session_name}...")
        await client.connect()

        if await client.is_user_authorized():
            print(
                f"⚠️ Session '{session_name}' is already authorized. "
                "This should not happen for a new session."
            )
            return False

        print("\n📱 Telegram login required.")
        print("   Step 1/3: enter the phone number of the Telegram account.")
        phone = await _prompt("Enter Phone Number (e.g., +989123456789): ")
        if not phone:
            print("❌ Phone number cannot be empty.")
            return False

        await client.send_code_request(phone)

        print("\n📨 A Telegram verification code has been sent.")
        code = await _prompt("Step 2/3 - Enter the verification code: ")
        if not code:
            print("❌ Verification code cannot be empty.")
            return False

        try:
            await client.sign_in(phone=phone, code=code)
        except errors.SessionPasswordNeededError:
            print("\n🔐 This account has Two-Step Verification enabled.")
            password = await _prompt("Step 3/3 - Enter your Telegram 2FA password: ")
            if not password:
                print("❌ Two-Step Verification password cannot be empty.")
                return False
            await client.sign_in(password=password)

        if not await client.is_user_authorized():
            print("❌ Telegram login did not complete successfully.")
            return False

        me = await client.get_me()
        _save_meta(
            session_name,
            {
                "status": "active",
                "telegram_user_id": getattr(me, "id", None),
                "username": getattr(me, "username", None),
                "phone": getattr(me, "phone", None),
            },
        )

        print(f"✅ Account '{session_name}' successfully authorized!")
        return True

    except errors.PhoneNumberInvalidError:
        print("❌ The phone number is invalid. Include the country code, e.g. +1... or +98...")
        return False
    except errors.PhoneCodeInvalidError:
        print("❌ The Telegram verification code is invalid.")
        return False
    except errors.PhoneCodeExpiredError:
        print("❌ The Telegram verification code has expired. Start login again.")
        return False
    except errors.FloodWaitError as exc:
        print(f"❌ Telegram rate limit: wait {exc.seconds} seconds before trying again.")
        return False
    except (EOFError, KeyboardInterrupt):
        print("\n⚠️ Interactive input was interrupted. Telegram login was cancelled.")
        return False
    except Exception as exc:
        log_exception(exc, f"Failed to register new account: {session_name}")
        print(f"❌ Error during registration. Check {config.ERROR_LOG_FILE}")
        return False
    finally:
        if client.is_connected():
            await client.disconnect()
        _client_cache.pop(session_name, None)
