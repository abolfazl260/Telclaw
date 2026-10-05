import asyncio
import json
import os
import time
from urllib.parse import urlparse

from telethon import TelegramClient, errors

import config
from error_handler import log_exception

os.makedirs(config.SESSION_DIR, exist_ok=True)


def _meta_path(session_name):
    return os.path.join(config.SESSION_DIR, f"{session_name}.meta.json")


def _session_path(session_name):
    return os.path.join(config.SESSION_DIR, session_name)


def _normalize_session_name(account):
    if isinstance(account, dict):
        account = account.get("session")
    if not isinstance(account, str):
        raise TypeError(f"session_name must be a string, got {type(account).__name__}")
    session_name = account.strip()
    if not session_name:
        raise ValueError("session_name is empty")
    return session_name


def _telegram_proxy():
    value = getattr(config, "TELEGRAM_PROXY", "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme.lower() != "socks5":
        raise ValueError("TELECLAW_TELEGRAM_PROXY must use socks5://host:port")
    if not parsed.hostname or not parsed.port:
        raise ValueError("TELECLAW_TELEGRAM_PROXY must include proxy host and port")
    import socks
    return (socks.SOCKS5, parsed.hostname, parsed.port, True, parsed.username, parsed.password)


def _load_meta(session_name):
    try:
        path = _meta_path(session_name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as exc:
        log_exception(exc, "Error loading session metadata")
    return {}


def _save_meta(session_name, meta):
    try:
        with open(_meta_path(session_name), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        log_exception(exc, "Error saving session metadata")


def _remove_session_files(session_name):
    """Remove a local session that cannot be proven to be authorized."""
    for path in (_session_path(session_name) + ".session", _meta_path(session_name)):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            log_exception(exc, f"Error removing invalid session: {session_name}")


async def get_active_accounts():
    """Return only sessions currently authorized in Telegram.

    A .session file alone is not enough. Each candidate is connected to
    Telegram and checked with is_user_authorized(). Invalid/logged-out
    sessions are removed and never shown in the account selector.
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
        # A Back Office registration deliberately owns an unauthorized session
        # between the code and optional 2FA steps. Do not treat it as stale,
        # but clean up abandoned registrations once their short TTL expires.
        registration = _registration_cache.get(session_name)
        if registration is not None:
            if _registration_expired(registration):
                await _discard_registration(session_name)
            else:
                continue
        client = None
        preexisting_client = session_name in _client_cache
        try:
            client = create_client(session_name)
            if not client.is_connected():
                await client.connect()
            authorized = await client.is_user_authorized()
            if not authorized:
                # Telegram explicitly confirmed the cached/on-disk session is
                # unauthorized. Disconnect and evict even when another caller
                # had previously cached this client; otherwise a deleted session
                # could remain usable through the stale in-process object.
                try:
                    if client.is_connected():
                        await client.disconnect()
                finally:
                    _client_cache.pop(session_name, None)
                _remove_session_files(session_name)
                continue

            meta = _load_meta(session_name)
            meta["status"] = "active"
            accounts.append({"session": session_name, "meta": meta})
        except Exception as exc:
            # A transient Telegram/network/proxy failure does not prove that a
            # local session is invalid. Keep the session file and simply omit it
            # from this live availability snapshot. We only delete a session
            # after Telegram explicitly confirms it is unauthorized above.
            log_exception(exc, f"Unable to verify Telegram session: {session_name}")
        finally:
            # Do not disconnect a client already owned by the running crawler/TUI.
            if client is not None and not preexisting_client:
                try:
                    if client.is_connected():
                        await client.disconnect()
                except Exception:
                    pass
                _client_cache.pop(session_name, None)

    return accounts


async def get_available_accounts():
    """Backward-compatible async alias."""
    return await get_active_accounts()


_client_cache = {}
_registration_cache = {}
_REGISTRATION_TTL_SECONDS = 600


def _registration_expired(state):
    return (time.monotonic() - float(state.get("started_monotonic") or 0)) > _REGISTRATION_TTL_SECONDS


async def _discard_registration(session_name, *, remove_session=True):
    state = _registration_cache.pop(session_name, None)
    client = (state or {}).get("client") or _client_cache.get(session_name)
    if client is not None:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass
    _client_cache.pop(session_name, None)
    if remove_session:
        _remove_session_files(session_name)




def create_client(account_name):
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


async def _prompt(prompt):
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
        print(f"\n[🔄] Connecting to Telegram for new session: {session_name}{proxy_status}...")
        await client.connect()

        if await client.is_user_authorized():
            print(f"⚠️ Session '{session_name}' is already authorized. This should not happen for a new session.")
            return False

        print("\n📱 Telegram login required.")
        phone = await _prompt("Enter Phone Number (e.g., +989123456789): ")
        if not phone:
            print("❌ Phone number cannot be empty.")
            return False
        await client.send_code_request(phone)

        code = await _prompt("Step 2/3 - Enter the verification code: ")
        if not code:
            print("❌ Telegram verification code cannot be empty.")
            return False

        try:
            await client.sign_in(phone=phone, code=code)
        except errors.SessionPasswordNeededError:
            password = await _prompt("Step 3/3 - Enter your Telegram 2FA password: ")
            if not password:
                print("❌ Two-Step Verification password cannot be empty.")
                return False
            await client.sign_in(password=password)

        if not await client.is_user_authorized():
            print("❌ Telegram login did not complete successfully.")
            return False

        me = await client.get_me()
        _save_meta(session_name, {
            "status": "active",
            "telegram_user_id": getattr(me, "id", None),
            "username": getattr(me, "username", None),
            "phone": getattr(me, "phone", None),
        })
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
        print(f"❌ Could not connect to Telegram after multiple attempts. {proxy_hint}")
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


async def begin_account_registration(session_name, phone):
    """Begin non-interactive Telegram login for a Back Office multi-step form."""
    session_name = _normalize_session_name(session_name)
    phone = str(phone or "").strip()
    if not phone:
        raise ValueError("Phone number cannot be empty")
    previous = _registration_cache.get(session_name)
    if previous is not None:
        await _discard_registration(session_name)
    if os.path.exists(f"{_session_path(session_name)}.session"):
        raise ValueError(f"Session '{session_name}' already exists")

    client = create_client(session_name)
    try:
        await client.connect()
        if await client.is_user_authorized():
            raise ValueError(f"Session '{session_name}' is already authorized")
        sent = await client.send_code_request(phone)
        _registration_cache[session_name] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": getattr(sent, "phone_code_hash", None),
            "stage": "code",
            "started_monotonic": time.monotonic(),
        }
        return {"session": session_name, "stage": "code"}
    except errors.PhoneNumberInvalidError as exc:
        await _discard_registration(session_name)
        raise ValueError("The phone number is invalid; include the country code") from exc
    except errors.FloodWaitError as exc:
        await _discard_registration(session_name)
        raise RuntimeError(f"Telegram rate limit: wait {exc.seconds} seconds") from exc
    except Exception:
        await _discard_registration(session_name)
        raise


async def _registration_state_or_raise(session_name):
    session_name = _normalize_session_name(session_name)
    state = _registration_cache.get(session_name)
    if state is None:
        raise ValueError("No pending Telegram account registration")
    if _registration_expired(state):
        await _discard_registration(session_name)
        raise ValueError("Telegram account registration expired; start again")
    return session_name, state


async def _finish_account_registration(session_name, state):
    client = state["client"]
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram login did not complete successfully")
    me = await client.get_me()
    _save_meta(session_name, {
        "status": "active",
        "telegram_user_id": getattr(me, "id", None),
        "username": getattr(me, "username", None),
        "phone": getattr(me, "phone", None),
    })
    _registration_cache.pop(session_name, None)
    if client.is_connected():
        await client.disconnect()
    _client_cache.pop(session_name, None)
    return {"session": session_name, "stage": "complete"}


async def submit_account_registration_code(session_name, code):
    session_name, state = await _registration_state_or_raise(session_name)
    if state.get("stage") != "code":
        raise ValueError("Telegram registration is not waiting for a code")
    code = str(code or "").strip()
    if not code:
        raise ValueError("Verification code cannot be empty")
    client = state["client"]
    try:
        await client.sign_in(
            phone=state["phone"],
            code=code,
            phone_code_hash=state.get("phone_code_hash"),
        )
    except errors.SessionPasswordNeededError:
        state["stage"] = "password"
        return {"session": session_name, "stage": "password"}
    except errors.PhoneCodeInvalidError as exc:
        raise ValueError("The Telegram verification code is invalid") from exc
    except errors.PhoneCodeExpiredError as exc:
        await _discard_registration(session_name)
        raise ValueError("The Telegram verification code expired; start again") from exc
    except errors.FloodWaitError as exc:
        raise RuntimeError(f"Telegram rate limit: wait {exc.seconds} seconds") from exc
    return await _finish_account_registration(session_name, state)


async def submit_account_registration_password(session_name, password):
    session_name, state = await _registration_state_or_raise(session_name)
    if state.get("stage") != "password":
        raise ValueError("Telegram registration is not waiting for a 2FA password")
    password = str(password or "")
    if not password:
        raise ValueError("Two-step verification password cannot be empty")
    try:
        await state["client"].sign_in(password=password)
    except errors.PasswordHashInvalidError as exc:
        raise ValueError("The Telegram two-step verification password is invalid") from exc
    except errors.FloodWaitError as exc:
        raise RuntimeError(f"Telegram rate limit: wait {exc.seconds} seconds") from exc
    return await _finish_account_registration(session_name, state)


async def cancel_account_registration(session_name):
    session_name = _normalize_session_name(session_name)
    existed = session_name in _registration_cache
    if existed:
        await _discard_registration(session_name)
    return existed


def get_account_registration_state(session_name=None):
    """Return sanitized in-memory registration state; never expose phone/code/password."""
    if session_name is not None:
        name = _normalize_session_name(session_name)
        state = _registration_cache.get(name)
        if state is None or _registration_expired(state):
            return None
        return {"session": name, "stage": state.get("stage")}
    for name, state in list(_registration_cache.items()):
        if not _registration_expired(state):
            return {"session": name, "stage": state.get("stage")}
    return None
