"""Application validation and administrator intent, independent of HTTP handlers."""
import asyncio
import json
from datetime import timedelta

import aiohttp
from . import recurrence
from .models import utcnow
from .telegram_sender import PreflightRetry
from .validation import ValidationError, content, buttons
from storage.database import record_system_activity


class Service:
    def __init__(self, repository, sender):
        self.repo, self.sender = repository, sender

    def payload(self, data):
        asset_key = str(data.get('asset_key') or '').strip()
        if asset_key:
            self.repo.media(asset_key)
        mode = data.get('parse_mode') or 'HTML'
        text, _ = content(data.get('text'), mode, bool(asset_key))
        return dict(text=text, parse_mode=mode, buttons=buttons(data.get('buttons') or []), asset_key=asset_key)

    def save(self, data, actor):
        return self.repo.save_message(data.get('title'), self.payload(data), actor,
            int(data['id']) if data.get('id') else None, data.get('version'))

    def duplicate(self, message_id, actor):
        row = self.repo.get_message(message_id)
        return self.repo.save_message((row['title'] + ' (copy)')[:200], row['content'], actor)

    async def checked_targets(self, ids):
        ids = sorted(set(int(x) for x in ids))
        if not 1 <= len(ids) <= 50:
            raise ValidationError('Select 1–50 eligible destinations')
        registry = {t['id']: t for t in self.repo.targets()}
        targets = []
        for target_id in ids:
            target = registry.get(target_id)
            if not target or not target['mm_eligible']:
                raise ValidationError('Destination is not eligible for Message Manager')
            targets.append(target)
        semaphore = asyncio.Semaphore(4)

        async def check(target):
            async with semaphore:
                try:
                    await self.sender.check_target(target)
                except (aiohttp.ClientError, TimeoutError, ValueError, PreflightRetry) as exc:
                    raise ValidationError(f"Cannot activate {target['label']}: {type(exc).__name__}") from exc

        results = await asyncio.gather(*(check(target) for target in targets), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                raise result
        return targets

    async def activate(self, message_id, spec, target_ids, actor, confirmed=False, immediate=False, replace_id=None):
        if not confirmed:
            raise ValidationError('Explicit publishing/scheduling confirmation is required')
        message = self.repo.get_message(message_id)
        self.payload(message['content'])
        targets = await self.checked_targets(target_ids)
        if immediate:
            # Immediate jobs still flow through the persistent worker, never send in the request.
            local = utcnow().astimezone(recurrence.TEHRAN)
            spec = recurrence.normalize({'date': local.date().isoformat(), 'time': local.strftime('%H:%M'), 'recurrence': 'once'})
        else:
            spec = recurrence.normalize(spec)
            if recurrence.at(spec, 0) < utcnow() - timedelta(minutes=1):
                raise ValidationError('Choose a future start date/time in Asia/Tehran')
        with self.repo.connection(True) as c:
            if replace_id:
                old = c.execute('SELECT message_id,state FROM mm_schedules WHERE id=?', (replace_id,)).fetchone()
                if not old or old['message_id'] != message_id or old['state'] not in {'active', 'paused'}:
                    raise ValidationError('Only this message’s active/paused schedules may be rescheduled')
                self.repo._cancel(c, replace_id)
            schedule_id = self.repo.schedule(message_id, spec, targets, actor, c)
        record_system_activity('message_manager', source='backoffice', message='Administrator activated a publication', details={'admin_id': actor, 'schedule_id': schedule_id})
        return schedule_id
