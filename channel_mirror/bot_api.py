"""Bot API adapter: distinguish definite rejection from ambiguous mutation."""

import aiohttp

from delivery import bot_limits


class APIError(RuntimeError):
    def __init__(self, message, *, code=0, retry_after=0, uncertain=False, message_ids=()):
        super().__init__(message)
        self.code, self.retry_after, self.uncertain = code, retry_after, uncertain
        self.message_ids = message_ids


class BotAPI:
    def __init__(self, token, *, session_factory=aiohttp.ClientSession):
        self.token, self.session_factory = token, session_factory

    async def call(self, method, payload, *, files=(), guard=None):
        mutating = method.startswith(("copy", "send", "edit"))
        try:
            if mutating:
                cost = len(payload.get("message_ids", payload.get("media", []))) or 1
                await bot_limits.wait_turn(self.token, payload.get("chat_id"), cost)
            elif bot_limits.cooldown_seconds(self.token):
                raise bot_limits.BotCooldown(bot_limits.cooldown_seconds(self.token))
        except bot_limits.BotCooldown as exc:
            raise APIError(str(exc), code=429, retry_after=exc.seconds) from exc
        if guard is not None and not guard():
            raise APIError("Mirror rule paused/disabled or claim expired before request", code=409, retry_after=30)
        handles = []
        try:
            kwargs = {"json": payload}
            if files:
                import json

                form = aiohttp.FormData()
                for name, value in payload.items():
                    form.add_field(name, json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value))
                for name, path, mime in files:
                    handle = path.open("rb")
                    handles.append(handle)
                    form.add_field(name, handle, filename=path.name, content_type=mime or "application/octet-stream")
                kwargs = {"data": form}
            timeout = aiohttp.ClientTimeout(total=90 if files else 45 if mutating else 15)
            async with self.session_factory(timeout=timeout) as session:
                if guard is not None and not guard():
                    raise APIError("Claim changed before sending", code=409, retry_after=30)
                async with session.post(f"https://api.telegram.org/bot{self.token}/{method}", **kwargs) as response:
                    try:
                        body = await response.json(content_type=None)
                    except (ValueError, aiohttp.ClientError):
                        raise APIError("Unreadable Telegram response", uncertain=mutating)
                    if not isinstance(body, dict) or type(body.get("ok")) is not bool:
                        raise APIError("Unconfirmed Telegram response", uncertain=mutating)
                    if body["ok"] is True:
                        if body.get("result") is None or body.get("result") is False:
                            raise APIError("Missing Telegram result", uncertain=mutating)
                        return body.get("result")
                    code = body.get("error_code")
                    if type(code) is not int or code < 400:
                        raise APIError("Unconfirmed Telegram rejection", uncertain=mutating)
                    delay = 0
                    params = body.get("parameters")
                    if code == 429:
                        delay = bot_limits.note_429(self.token, params.get("retry_after", 60) if isinstance(params, dict) else 60)
                    elif isinstance(params, dict) and "retry_after" in params:
                        try:
                            delay = max(0, min(int(params["retry_after"]), 604800))
                        except (ValueError, TypeError):
                            delay = 0
                    error = str(body.get("description") or f"Telegram API {code}").replace(self.token, "[redacted]")[:1000]
                    raise APIError(error, code=code, retry_after=delay)
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            # Even connection failures may happen after the request was written.
            raise APIError("Telegram connection interrupted", uncertain=mutating) from exc
        finally:
            for handle in handles:
                handle.close()
