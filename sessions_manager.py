import asyncio
import json
import os
from urllib.parse import urlparse

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


def _telegram_proxy():
    """Build a Telethon SOCKS5 proxy tuple from configuration, if configured."""
    value = getattr(config, "TELEGRAM_PROXY", "").strip()
    if not value:
        return None

    parsed = urlparse(value)
    if parsed.scheme.lower() != "socks5":
        raise ValueError("TELECLAW_TELEGRAM_PROXY must use socks5://host:port")
    if not parsed.hostname or not parsed.port:
        raise ValueError("TELECLAW_TELEGRAM_PROXY must include proxy host and port")

    import socks

    return (
        socks.SOCKS5,
        parsed.hostname,
        parsed.port,
        True,
        parsed.username,
        parsed.password,
    )


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


def _remove_session_files(session_name):
    """Remove an invalid/unauthorized local session and its metadata."""
    for path in (_session_path(session_name) + ".session", _meta_path(session_name)):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            log_exception(exc, f"Error removing invalid session: {session_name}")


def get_active_accounts():
    """Return only sessions that are actually authorized in Telegram.

    A local .session file alone is not enough: Telethon must successfully
    connect and report is_user_authorized(). Invalid/expired local sessions
    are removed so the account selector never shows fake test names.
    """
    accounts = []
    try:
        filenames = sorted(os.listdir(config.SESSION_DIR))
    except Exception as exc:
        log_exception(exc, "Error while listing active sessions")
        return []

    for filename in filenames:
        if not filename.endswith(".session"):
            continue

        session_name = os.path.splitext(filename)[0]
        client = None
        try:
            client = create_client(session_name)
            connected = False

            async def _check_authorized():
                nonlocal connected
                await client.connect()
                connected = True
                return await client.is_user_authorized()

            # This function is synchronous because the UI calls it before
            # entering the account-selection flow. Use the running event loop
            # when available; otherwise run a short-lived loop.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                # get_active_accounts is not expected to be called from an
                # already-running loop; fail closed rather than reporting a
                # local session as active without Telegram authorization.
                authorized = False
            else:
                authorized = asyncio.run(_check_authorized())

            if not authorized:
                _remove_session_files(session_name)
                continue

            meta = _load_meta(session_name)
            meta["status"] = "active"
            accounts.append({"session": session_name, "meta": meta})
        except Exception as exc:
            # Any failure to prove authorization means the session must not be
            # offered to the user as an active crawler account.
            log_exception(exc, f"Inactive Telegram session: {session_name}")
            _remove_session_files(session_name)
        finally:
            if client is not None:
                try:
                    if client.is_connected():
                        # Safe cleanup for the synchronous discovery path.
                        try:
                            loop = asyncio.get_running_loop()
                        except RuntimeError:
                            loop = None
                        if not loop or not loop.is_running():
                            asyncio.run(client.disconnect())
                except Exception:
                    pass
                _client_cache.pop(session_name, None)

    return accounts


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

        client_kwargs = {}
        proxy = _telegram_proxy()
        if proxy is not None:
            client_kwargs["proxy"] = proxy

        client = TelegramClient(
            _session_path(session_name),
            config.API_ID,
            config.API_HASH,
            **client_kwargs,
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

    if os.path.exists(f"{_session_path(session_name)}.session"):
        print(
            f"❌ Session '{session_name}' already exists. "
            "Choose a different session name or select the existing account."
        )
        return False

    client = create_client(session_name)

    try:
        proxy_status = " via configured SOCKS5 proxy" if config.TELEGRAM_PROXY else " directly"
        print(
            f"\n[🔄] Connecting to Telegram for new session: {session_name}"
            f"{proxy_status}..."
        )
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
            print("❌ Telegram verification code cannot be empty.")
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
    except ConnectionError:
        proxy_hint = (
            "Check the configured SOCKS5 proxy and its host/port."
            if config.TELEGRAM_PROXY
            else "Check internet access to Telegram. If Telegram is blocked on this network, configure a SOCKS5 proxy with TELECLAW_TELEGRAM_PROXY."
        )
        print(
            "❌ Could not connect to Telegram after multiple attempts. "
            f"{proxy_hint}"
        )
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
