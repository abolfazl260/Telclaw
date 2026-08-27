"""Independent Telegram Bot API monitor for Telclaw."""
from __future__ import annotations
import asyncio, html, json, logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import aiohttp
import config
from storage import database
from services.stage_control import get_stage_control
logger=logging.getLogger(__name__)
TEHRAN_TZ=ZoneInfo("Asia/Tehran")
PROJECT_ROOT=Path(__file__).resolve().parent.parent
CRAWLER_ERRORS_LOG=PROJECT_ROOT / "crawler_errors.log"
TELEGRAM_MAX_DOCUMENT_BYTES=50*1024*1024

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
        commands=[{"command":"start","description":"فعال‌سازی دریافت گزارش‌ها"},{"command":"stop","description":"توقف دریافت گزارش‌ها"},{"command":"status","description":"نمایش وضعیت فعلی سیستم"},{"command":"health","description":"بررسی سلامت فعلی سیستم"},{"command":"today","description":"نمایش آمار امروز"},{"command":"source","description":"نمایش کانال‌ها و گروه‌های تحت کرال"},{"command":"down_errors","description":"Download crawler error log"},{"command":"database","description":"Download full SQLite database"}]
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
    async def _send_document(self,chat_id,file_path,caption):
        form=aiohttp.FormData(); file_handle=file_path.open("rb"); form.add_field("chat_id",str(chat_id)); form.add_field("caption",caption); form.add_field("document",file_handle,filename=file_path.name)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
                async with session.post(f"https://api.telegram.org/bot{self.token}/sendDocument",data=form) as response:
                    data=await response.json(content_type=None)
                    if not response.ok or not data.get("ok"): raise RuntimeError(f"Telegram API sendDocument failed: HTTP {response.status}")
                    return data
        finally: file_handle.close()
    async def _poll_updates(self):
        while not self._stopping.is_set():
            try:
                result=await self._api("getUpdates",{"offset":self._offset,"timeout":20,"allowed_updates":["message","callback_query"]})
                for update in result.get("result",[]): self._offset=max(self._offset,int(update["update_id"])+1); await self._handle_update(update)
            except asyncio.CancelledError: raise
            except Exception: logger.exception("Telegram monitor polling failed"); await asyncio.sleep(5)
    def _stage_keyboard(self):
        return {"inline_keyboard":[
            [{"text":"⏭️ Skip Crawl","callback_data":"skip:crawl"},{"text":"⏭️ Skip Processing","callback_data":"skip:processing"}],
            [{"text":"⏭️ Skip AI","callback_data":"skip:ai"},{"text":"⏭️ Skip Advertio","callback_data":"skip:advertio"}],
            [{"text":"🔄 Refresh Status","callback_data":"refresh:status"}],
        ]}
    async def _handle_callback(self,callback):
        callback_id=callback.get("id"); data=str(callback.get("data") or ""); message=callback.get("message") or {}; chat=(message.get("chat") or {}); chat_id=chat.get("id")
        if callback_id: await self._api("answerCallbackQuery",{"callback_query_id":callback_id})
        if chat_id is None: return
        if not self._is_subscribed(chat_id):
            await self._send(chat_id,"⛔ You are not subscribed to Telclaw monitoring.\n\nUse /start first."); return
        if data == "refresh:status":
            await self._send(chat_id,await self._build_status_message(),reply_markup=self._stage_keyboard()); return
        if data.startswith("skip:"):
            stage=data.split(":",1)[1]
            if stage not in {"crawl","processing","ai","advertio"}: return
            get_stage_control().request_skip(stage)
            labels={"crawl":"کرال","processing":"پروسس","ai":"هوش مصنوعی","advertio":"Advertio"}
            await self._send(chat_id,f"⏭️ درخواست رد کردن مرحله <b>{labels[stage]}</b> ثبت شد.\n\nمرحله در اولین نقطه امن متوقف می‌شود و Pipeline به مرحله بعد می‌رود.")
    async def _handle_update(self,update):
        if update.get("callback_query"):
            await self._handle_callback(update["callback_query"]); return
        message=update.get("message") or {}; chat=message.get("chat") or {}; chat_id=chat.get("id")
        if chat_id is None:return
        text=(message.get("text") or "").strip().lower()
        command=text.split(maxsplit=1)[0].split("@",1)[0] if text else ""
        if command == "/start":
            database.subscribe_monitor_chat(int(chat_id),chat.get("username"),chat.get("first_name")); await self._send(chat_id,"✅ Telclaw monitoring فعال شد.\nاز این پس خطاها و گزارش‌های سیستم برای شما ارسال می‌شود.")
        elif command == "/stop":
            database.unsubscribe_monitor_chat(int(chat_id)); await self._send(chat_id,"⛔ دریافت گزارش‌های Telclaw متوقف شد.")
        elif command == "/status": await self._send(chat_id,await self._build_status_message(),reply_markup=self._stage_keyboard())
        elif command == "/health": await self._send(chat_id,await self._build_health_message())
        elif command == "/today": await self._send(chat_id,await self._build_today_message())
        elif command == "/source": await self._send_source_chunks(chat_id)
        elif command == "/down_errors": await self._download_errors(chat_id)
        elif command == "/database": await self._download_database(chat_id)
    def _tehran_timestamp(self,value):
        if not value:return "هنوز ثبت نشده"
        try:
            dt=datetime.fromisoformat(str(value).replace("Z","+00:00"))
            if dt.tzinfo is None: dt=dt.replace(tzinfo=ZoneInfo("UTC"))
            return dt.astimezone(TEHRAN_TZ).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError,ValueError): return str(value)
    def _is_subscribed(self,chat_id): return any(int(s["chat_id"])==int(chat_id) for s in database.get_monitor_subscribers())
    async def _download_errors(self,chat_id):
        if not self._is_subscribed(chat_id): await self._send(chat_id,"⛔ You are not subscribed to Telclaw monitoring.\n\nUse /start first."); return
        log_path=CRAWLER_ERRORS_LOG
        try:
            if not log_path.exists() or not log_path.is_file(): await self._send(chat_id,"❌ crawler_errors.log not found."); return
            size=log_path.stat().st_size
            if size==0: await self._send(chat_id,"⚠️ crawler_errors.log is empty."); return
            if size>TELEGRAM_MAX_DOCUMENT_BYTES:
                await self._send(chat_id,"❌ <b>crawler_errors.log is too large to send via Telegram.</b>\n\n" f"File size: {size/(1024*1024):.2f} MB"); return
            timestamp=self._tehran_timestamp(datetime.now(ZoneInfo("UTC")).isoformat())
            await self._send_document(chat_id,log_path,"📝 Telclaw crawler error log\n" f"🕐 Generated: {timestamp} Tehran")
        except Exception:
            logger.exception("Failed to send crawler_errors.log to chat %s",chat_id); await self._send(chat_id,"❌ Failed to send crawler_errors.log.")

    async def _download_database(self,chat_id):
        if not self._is_subscribed(chat_id): await self._send(chat_id,"⛔ You are not subscribed to Telclaw monitoring.\n\nUse /start first."); return
        db_path=Path(config.DB_NAME)
        try:
            if not db_path.is_absolute(): db_path=PROJECT_ROOT / db_path
            db_path=db_path.resolve()
            if not db_path.exists() or not db_path.is_file(): await self._send(chat_id,"❌ Database file not found."); return
            size=db_path.stat().st_size
            if size==0: await self._send(chat_id,"⚠️ Database file is empty."); return
            if size>TELEGRAM_MAX_DOCUMENT_BYTES:
                await self._send(chat_id,"❌ <b>Database file is too large to send via Telegram.</b>\n\n" f"File size: {size/(1024*1024):.2f} MB"); return
            timestamp=self._tehran_timestamp(datetime.now(ZoneInfo("UTC")).isoformat())
            await self._send_document(chat_id,db_path,"🗄 Telclaw SQLite database\n" f"🕐 Generated: {timestamp} Tehran")
        except Exception:
            logger.exception("Failed to send database to chat %s",chat_id); await self._send(chat_id,"❌ Failed to send database file.")
    async def _build_status_message(self):
        status=database.get_pipeline_status(); last=status.get("last_crawl")
        return ("📊 <b>Telclaw Current Status</b>\n\n" f"🟢 <b>System:</b> {html.escape(str(status['system']))}\n" f"📥 <b>Collected:</b> {status['collected']}\n" f"⚙️ <b>Processing pending:</b> {status['processing_pending']}\n" f"⚙️ <b>Processing failed:</b> {status['processing_failed']}\n" f"🤖 <b>AI pending:</b> {status['ai_pending']}\n" f"🤖 <b>AI failed:</b> {status['ai_failed']}\n" f"📤 <b>Advertio pending:</b> {status['advertio_pending']}\n" f"📤 <b>Advertio failed:</b> {status['advertio_failed']}\n" f"📦 <b>Total messages:</b> {status['total_messages']}\n" f"📡 <b>Channels:</b> {status['channels']}\n" f"👥 <b>Active subscribers:</b> {status['subscribers']}\n" f"📥 <b>Last crawl:</b> {html.escape(self._tehran_timestamp(last))} Tehran")
    async def _build_health_message(self):
        health=database.get_pipeline_health(); icon=lambda state:"🟢" if state=="HEALTHY" else ("🟡" if state=="WARNING" else "🔴")
        lines=["🏥 <b>TELCLAW SYSTEM HEALTH</b>","",f"📥 <b>Crawler activity:</b> {icon(health['crawler'])} {health['crawler']}",f"⚙️ <b>Processing:</b> {icon(health['processing'])} {health['processing']}",f"🤖 <b>AI:</b> {icon(health['ai'])} {health['ai']}",f"📤 <b>Advertio:</b> {icon(health['advertio'])} {health['advertio']}",f"🗄 <b>Database:</b> {icon(health['database'])} {health['database']}","",f"📥 <b>Last crawl:</b> {html.escape(self._tehran_timestamp(health['last_crawl']))} Tehran",f"⚙️ <b>Last processing:</b> {html.escape(self._tehran_timestamp(health['last_processing']))} Tehran",f"🤖 <b>Last AI:</b> {html.escape(self._tehran_timestamp(health['last_ai']))} Tehran",f"📤 <b>Last Advertio:</b> {html.escape(self._tehran_timestamp(health['last_advertio']))} Tehran","",f"📦 <b>Pipeline backlog:</b> {health['backlog']}",f"🚨 <b>Failed items:</b> {health['failed']}"]
        if health['warning']: lines.extend(["",f"⚠️ <b>Warning:</b> {html.escape(health['warning'])}"])
        return "\n".join(lines)
    async def _build_today_message(self):
        today=datetime.now(TEHRAN_TZ).date().isoformat(); conn=database.get_connection()
        try:
            row=conn.execute("""SELECT COUNT(*) crawled,SUM(CASE WHEN collection_status='collected' THEN 1 ELSE 0 END) new_messages,SUM(CASE WHEN processing_status='processed' THEN 1 ELSE 0 END) processed,SUM(CASE WHEN ai_status='processed' THEN 1 ELSE 0 END) ai_processed,SUM(CASE WHEN ai_status='failed' THEN 1 ELSE 0 END) ai_failed,SUM(CASE WHEN advertio_status='sent' THEN 1 ELSE 0 END) advertio_sent,SUM(CASE WHEN advertio_status='failed' THEN 1 ELSE 0 END) advertio_failed FROM messages WHERE date LIKE ?""",(today+"%",)).fetchone()
            def n(key): return int(row[key] or 0)
            return ("📊 <b>TODAY</b>\n\n" f"📥 <b>Crawled:</b> {n('crawled'):,}\n" f"🆕 <b>New messages:</b> {n('new_messages'):,}\n" f"⚙️ <b>Processed:</b> {n('processed'):,}\n" f"🤖 <b>AI processed:</b> {n('ai_processed'):,}\n" f"❌ <b>AI failed:</b> {n('ai_failed'):,}\n" f"📤 <b>Advertio sent:</b> {n('advertio_sent'):,}\n" f"🚨 <b>Advertio failed:</b> {n('advertio_failed'):,}\n\n" f"🕐 <b>Tehran date:</b> {today}")
        finally: conn.close()
    async def _send_source_chunks(self,chat_id):
        try:
            with open(config.CHANNELS_JSON,"r",encoding="utf-8") as f:data=json.load(f)
        except Exception as exc: await self._send(chat_id,f"❌ <b>Source file error</b>\n<pre>{html.escape(str(exc))}</pre>"); return
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
    async def _send(self,chat_id,text,reply_markup=None):
        payload={"chat_id":chat_id,"text":text,"parse_mode":"HTML","disable_web_page_preview":True}
        if reply_markup is not None: payload["reply_markup"]=reply_markup
        await self._api("sendMessage",payload)
    async def broadcast(self,text):
        if not self.enabled:return
        for subscriber in database.get_monitor_subscribers():
            try: await self._send(int(subscriber["chat_id"]),text)
            except Exception: logger.warning("Telegram monitor delivery failed for subscriber %s",subscriber["chat_id"])
    async def error(self,level,source,message): await self.broadcast(f"🚨 <b>Telclaw System Error</b>\n\n<b>Level:</b> {html.escape(level)}\n<b>Source:</b> {html.escape(source)}\n<b>Time:</b> {self._tehran_timestamp(datetime.utcnow().isoformat())} Tehran\n\n<pre>{html.escape(message[:3500])}</pre>")
    async def report(self,kind,stats):
        titles={"crawl":"📥 CRAWL REPORT","processing":"⚙️ PROCESSING REPORT","ai":"🤖 AI REPORT","advertio":"📤 ADVERTIO REPORT"}; lines=[f"<b>{titles.get(kind,kind.upper()+' REPORT')}</b>",f"🕐 <b>Time:</b> {self._tehran_timestamp(datetime.utcnow().isoformat())} Tehran"]
        for key,value in stats.items():lines.append(f"<b>{html.escape(str(key))}:</b> {html.escape(str(value))}")
        await self.broadcast("\n".join(lines))
_monitor=None
def get_telegram_monitor():
    global _monitor
    if _monitor is None:_monitor=TelegramMonitor()
    return _monitor
