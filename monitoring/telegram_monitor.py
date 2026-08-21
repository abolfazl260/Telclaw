"""Independent Telegram Bot API monitor for Telclaw."""
from __future__ import annotations
import asyncio, html, json, logging
from datetime import datetime
from zoneinfo import ZoneInfo
import aiohttp
import config
from storage import database
logger=logging.getLogger(__name__)
TEHRAN_TZ=ZoneInfo("Asia/Tehran")

class _TelegramErrorHandler(logging.Handler):
    def __init__(self,monitor): super().__init__(level=logging.ERROR); self.monitor=monitor
    def emit(self,record):
        if record.name.startswith("monitoring.telegram_monitor"): return
        try: asyncio.get_running_loop().create_task(self.monitor.error(record.levelname,record.name,self.format(record)))
        except RuntimeError: pass

class TelegramMonitor:
    def __init__(self):
        self.token=config.TELEGRAM_BOT_TOKEN; self.enabled=config.TELEGRAM_MONITOR_ENABLED and bool(self.token); self._task=None; self._offset=0; self._stopping=asyncio.Event(); self._error_handler=None
    async def start(self):
        if not self.enabled: logger.info("Telegram monitor disabled"); return
        database.initialize_db(); self._stopping.clear(); self._error_handler=_TelegramErrorHandler(self); self._error_handler.setFormatter(logging.Formatter("%(message)s")); logging.getLogger().addHandler(self._error_handler); await self._register_commands(); self._task=asyncio.create_task(self._poll_updates(),name="telegram-monitor-poll"); logger.info("Telegram monitoring bot started")
    async def _register_commands(self):
        commands=[{"command":"start","description":"فعال‌سازی دریافت گزارش‌ها"},{"command":"stop","description":"توقف دریافت گزارش‌ها"},{"command":"status","description":"نمایش وضعیت فعلی سیستم"},{"command":"source","description":"نمایش کانال‌ها و گروه‌های تحت کرال"}]
        try: await self._api("setMyCommands",{"commands":commands}); logger.info("Telegram monitor commands registered")
        except Exception: logger.exception("Failed to register Telegram monitor commands")
    async def stop(self):
        self._stopping.set()
        if self._error_handler: logging.getLogger().removeHandler(self._error_handler); self._error_handler=None
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
            self._task=None
    async def _api(self,method,payload=None):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
            async with session.post(f"https://api.telegram.org/bot{self.token}/{method}",json=payload or {}) as response:
                data=await response.json(content_type=None)
                if not response.ok or not data.get("ok"): raise RuntimeError(f"Telegram API {method} failed: HTTP {response.status}")
                return data
    async def _poll_updates(self):
        while not self._stopping.is_set():
            try:
                result=await self._api("getUpdates",{"offset":self._offset,"timeout":20,"allowed_updates":["message"]})
                for update in result.get("result",[]): self._offset=max(self._offset,int(update["update_id"])+1); await self._handle_update(update)
            except asyncio.CancelledError: raise
            except Exception: logger.exception("Telegram monitor polling failed"); await asyncio.sleep(5)
    async def _handle_update(self,update):
        message=update.get("message") or {}; chat=message.get("chat") or {}; chat_id=chat.get("id")
        if chat_id is None:return
        text=(message.get("text") or "").strip().lower()
        if text.startswith("/start"):
            database.subscribe_monitor_chat(int(chat_id),chat.get("username"),chat.get("first_name")); await self._send(chat_id,"✅ Telclaw monitoring فعال شد.\nاز این پس خطاها و گزارش‌های سیستم برای شما ارسال می‌شود.")
        elif text.startswith("/stop"):
            database.unsubscribe_monitor_chat(int(chat_id)); await self._send(chat_id,"⛔ دریافت گزارش‌های Telclaw متوقف شد.")
        elif text.startswith("/status"): await self._send(chat_id,await self._build_status_message())
        elif text.startswith("/source"): await self._send_source_chunks(chat_id)
    def _tehran_now(self): return datetime.now(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    async def _build_status_message(self):
        status=database.get_pipeline_status(); last=status.get("last_crawl") or "هنوز کرالی ثبت نشده"
        return ("📊 <b>Telclaw Current Status</b>\n\n" f"🟢 <b>System:</b> {html.escape(str(status['system']))}\n" f"📥 <b>Collected:</b> {status['collected']}\n" f"⚙️ <b>Processing pending:</b> {status['processing_pending']}\n" f"⚙️ <b>Processing failed:</b> {status['processing_failed']}\n" f"🤖 <b>AI pending:</b> {status['ai_pending']}\n" f"🤖 <b>AI failed:</b> {status['ai_failed']}\n" f"📤 <b>Advertio pending:</b> {status['advertio_pending']}\n" f"📤 <b>Advertio failed:</b> {status['advertio_failed']}\n" f"📦 <b>Total messages:</b> {status['total_messages']}\n" f"📡 <b>Channels:</b> {status['channels']}\n" f"👥 <b>Active subscribers:</b> {status['subscribers']}\n" f"🕐 <b>Current Tehran time:</b> {self._tehran_now()}\n" f"📥 <b>Last crawl:</b> {html.escape(str(last))} Tehran\n\n" f"🕐 <b>Checked:</b> {self._tehran_now()} Tehran")
    async def _send_source_chunks(self,chat_id):
        try:
            with open(config.CHANNELS_JSON,"r",encoding="utf-8") as f:data=json.load(f)
        except Exception as exc:
            await self._send(chat_id,f"❌ <b>Source file error</b>\n<pre>{html.escape(str(exc))}</pre>"); return
        lines=[f"📡 <b>Configured Sources</b>",f"<b>File:</b> {html.escape(config.CHANNELS_JSON)}",""]; total=0
        for category,items in data.items():
            lines.append(f"<b>{html.escape(str(category))}</b>")
            for item in items:
                username=str(item.get("username","")).lstrip("@"); name=html.escape(str(item.get("name") or username or "Unknown"))
                if username: lines.append(f'• <a href="https://t.me/{html.escape(username,quote=True)}">{name}</a> (@{html.escape(username)})')
                else: lines.append(f"• {name}")
                total+=1
            lines.append("")
        lines.insert(1,f"<b>Total:</b> {total}"); chunks=[]; current=""
        for line in "\n".join(lines).splitlines(True):
            if current and len(current)+len(line)>3900: chunks.append(current); current=""
            current+=line
        if current:chunks.append(current)
        for chunk in chunks: await self._send(chat_id,chunk)
    async def _send(self,chat_id,text): await self._api("sendMessage",{"chat_id":chat_id,"text":text,"parse_mode":"HTML","disable_web_page_preview":True})
    async def broadcast(self,text):
        if not self.enabled:return
        for subscriber in database.get_monitor_subscribers():
            try: await self._send(int(subscriber["chat_id"]),text)
            except Exception: logger.warning("Telegram monitor delivery failed for subscriber %s",subscriber["chat_id"])
    async def error(self,level,source,message): await self.broadcast(f"🚨 <b>Telclaw System Error</b>\n\n<b>Level:</b> {html.escape(level)}\n<b>Source:</b> {html.escape(source)}\n<b>Time:</b> {self._tehran_now()} Tehran\n\n<pre>{html.escape(message[:3500])}</pre>")
    async def report(self,kind,stats):
        titles={"crawl":"📥 CRAWL REPORT","processing":"⚙️ PROCESSING REPORT","ai":"🤖 AI REPORT","advertio":"📤 ADVERTIO REPORT"}; lines=[f"<b>{titles.get(kind,kind.upper()+' REPORT')}</b>",f"🕐 <b>Time:</b> {self._tehran_now()} Tehran"]
        for key,value in stats.items():lines.append(f"<b>{html.escape(str(key))}:</b> {html.escape(str(value))}")
        await self.broadcast("\n".join(lines))
_monitor=None
def get_telegram_monitor():
    global _monitor
    if _monitor is None:_monitor=TelegramMonitor()
    return _monitor
