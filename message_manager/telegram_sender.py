"""Telegram transport. Only a structured, explicit rejection is retryable."""
import hashlib
import json
import math
from datetime import datetime
from contextlib import ExitStack

import aiohttp
import config
from .models import SendResult, utcnow, stamp
from .validation import ValidationError


class PreflightRetry(RuntimeError):
    """A read-only permission request failed; no publication was attempted."""
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class TelegramSender:
    def __init__(self, media, token=None, session_factory=aiohttp.ClientSession):
        self.media = media
        self.token = config.TELEGRAM_BOT_TOKEN if token is None else token
        self.session_factory = session_factory
        self.bot_id = None

    def cooldown_seconds(self):
        with self.media.repo.connection() as c:
            row = c.execute("SELECT MAX(value) FROM (SELECT rate_limited_until AS value FROM publishing_targets UNION ALL SELECT value FROM mm_runtime WHERE key='bot_cooldown')").fetchone()
        if row and row[0] and row[0] > stamp():
            return max(1, math.ceil((datetime.fromisoformat(row[0]) - utcnow()).total_seconds()))
        return 0

    async def _api(self, method, payload):
        cooldown = self.cooldown_seconds()
        if cooldown:
            raise PreflightRetry('Telegram bot is in shared rate-limit cooldown', cooldown)
        async with self.session_factory(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(f'https://api.telegram.org/bot{self.token}/{method}', json=payload) as response:
                try:
                    body = await response.json(content_type=None)
                except (ValueError, aiohttp.ClientError) as exc:
                    raise PreflightRetry('Unreadable permission-check response') from exc
                if not isinstance(body, dict):
                    raise PreflightRetry('Unexpected permission-check response')
                if response.status != 200 or not body.get('ok'):
                    code = int(body.get('error_code') or response.status)
                    if code == 429:
                        delay = max(1, min(int((body.get('parameters') or {}).get('retry_after') or 60), 604800))
                        self.media.repo.cooldown(delay)
                        raise PreflightRetry('Telegram permission check rate limited', delay)
                    if code >= 500:
                        raise PreflightRetry('Telegram permission check temporarily unavailable')
                    detail = str(body.get('description') or 'Telegram permission check failed')[:500]
                    raise ValidationError(detail.replace(self.token, '[redacted]'))
                return body['result']

    async def check_target(self, target):
        if not self.token:
            raise ValidationError('TELCLAW_TELEGRAM_BOT_TOKEN is required')
        if self.bot_id is None:
            self.bot_id = (await self._api('getMe', {}))['id']
        chat = await self._api('getChat', {'chat_id': target['chat_id']})
        member = await self._api('getChatMember', {'chat_id': target['chat_id'], 'user_id': self.bot_id})
        if chat.get('type') != 'channel' or member.get('status') not in {'creator', 'administrator'} or (member.get('status') == 'administrator' and member.get('can_post_messages') is not True):
            raise ValidationError('Bot must be an administrator with posting permission in a channel')
        return str(chat.get('title') or target['chat_id'])

    async def send(self, delivery):
        cooldown = self.cooldown_seconds()
        if cooldown:
            return SendResult('retry', 'Telegram bot is in shared rate-limit cooldown', retry_after=cooldown)
        data = delivery['content']
        method, field = 'sendMessage', 'text'
        media = None
        if data.get('asset_key'):
            try:
                media = self.media.repo.media(data['asset_key'])
                method = {'photo': 'sendPhoto', 'video': 'sendVideo', 'document': 'sendDocument'}[media['kind']]
                field = 'caption'
            except (ValueError, KeyError):
                return SendResult('failed', 'Media asset is unavailable')
        payload = {'chat_id': delivery['chat_id'], field: data['text']}
        if data['parse_mode'] == 'HTML':
            payload['parse_mode'] = 'HTML'
        if data['buttons']:
            payload['reply_markup'] = {'inline_keyboard': data['buttons']}
        # File IDs are tied to a bot. Store a bot fingerprint alongside the cached ID.
        fingerprint = hashlib.sha256(self.token.encode()).hexdigest()[:16]
        with ExitStack() as stack:
            if media:
                media_field = media['kind']
                cached = media['file_id'].split(':', 1)
                if len(cached) == 2 and cached[0] == fingerprint:
                    payload[media_field] = cached[1]
                else:
                    try:
                        path = self.media.path(media)
                        handle = stack.enter_context(path.open('rb'))
                    except (OSError, ValidationError):
                        return SendResult('failed', 'Private media file is unavailable')
                    form = aiohttp.FormData()
                    for k, v in payload.items():
                        form.add_field(k, json.dumps(v) if isinstance(v, dict) else str(v))
                    form.add_field(media_field, handle, filename=media['original_name'], content_type=media['mime'])
                    payload = form
            try:
                async with self.session_factory(timeout=aiohttp.ClientTimeout(total=45)) as session:
                    kwargs = {'data': payload} if isinstance(payload, aiohttp.FormData) else {'json': payload}
                    async with session.post(f'https://api.telegram.org/bot{self.token}/{method}', **kwargs) as response:
                        try:
                            body = await response.json(content_type=None)
                        except (ValueError, aiohttp.ClientError):
                            return SendResult('uncertain', 'Telegram returned an unreadable response; verify before resending')
                        if not isinstance(body, dict):
                            return SendResult('uncertain', 'Unexpected Telegram response; verify before resending')
                        if body.get('ok') is True:
                            result = body.get('result') or {}
                            message_id = result.get('message_id')
                            if not isinstance(message_id, int) or message_id <= 0:
                                return SendResult('uncertain', 'Telegram acknowledged without a message ID')
                            chat = result.get('chat') or {}
                            username = chat.get('username')
                            chat_id = str(chat.get('id', delivery['chat_id']))
                            link = f'https://t.me/{username}/{message_id}' if username else (
                                f'https://t.me/c/{chat_id[4:]}/{message_id}' if chat_id.startswith('-100') else '')
                            file_id = ''
                            if media:
                                item = result.get(media['kind']) or {}
                                if isinstance(item, list):
                                    item = item[-1] if item else {}
                                if item.get('file_id'):
                                    file_id = fingerprint + ':' + item['file_id']
                            return SendResult('sent', message_id=message_id, message_link=link, file_id=file_id)
                        # A structured Bot API error confirms no successful publication.
                        if body.get('ok') is not False:
                            return SendResult('uncertain', 'Telegram did not confirm success or rejection')
                        code = int(body.get('error_code') or response.status)
                        error = str(body.get('description') or f'Telegram HTTP {code}')[:1500].replace(self.token, '[redacted]')
                        if code == 429:
                            delay = max(1, min(int((body.get('parameters') or {}).get('retry_after') or 60), 604800))
                            return SendResult('retry', error, retry_after=delay)
                        if 500 <= code <= 599:
                            return SendResult('retry', error)
                        return SendResult('failed', error)
            except aiohttp.ClientConnectorError:
                return SendResult('retry', 'Connection could not be established')
            except (aiohttp.ClientError, TimeoutError, OSError):
                return SendResult('uncertain', 'Connection interrupted or timed out; Telegram may have accepted the message')
