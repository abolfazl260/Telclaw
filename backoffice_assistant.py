"""Authenticated back-office interface for the read-only database analyst."""
import asyncio
import html
import json
import sqlite3
from aiohttp import web
from ai.database_analyst import AnalysisError, DatabaseAnalyst

ANALYST_KEY = web.AppKey('database_analyst', DatabaseAnalyst)


async def assistant_page(request):
    csrf = html.escape(request['session']['csrf'], quote=True)
    nonce = html.escape(request['csp_nonce'], quote=True)
    return web.Response(text='''<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Telclaw · دستیار تحلیل</title>
<style>body{font:16px system-ui;background:#f3f6fa;color:#172b46;max-width:1000px;margin:auto;padding:24px}
nav{display:flex;gap:16px;flex-wrap:wrap}section{background:white;padding:24px;margin-top:20px;border-radius:14px}
textarea{width:100%;box-sizing:border-box;min-height:100px;font:inherit;padding:12px}button{padding:10px 18px;margin:8px;font:inherit;cursor:pointer}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}article{border-top:1px solid #d8e0ea;padding:12px 0}details{font-size:14px}#status{color:#475569}</style>
<nav><a href="/">انتشار</a><a href="/operations">عملیات</a><a href="/data">دیتابیس</a><a href="/health">سلامت سیستم</a></nav>
<section><h1>دستیار تحلیل دیتابیس</h1><p>با سرویس‌های AI تنظیم‌شده، پیام‌ها و علت خطاها را بررسی کن. دسترسی فقط‌خواندنی است.
متن رکوردهای مرتبط به سرویس AI ارسال می‌شود و از سهمیهٔ آن مصرف می‌کند. پاسخ مدل را با شواهد بررسی کن.</p>
<form id="chat"><input type="hidden" name="csrf" value="''' + csrf + '''">
<textarea name="question" maxlength="2000" required placeholder="چرا Jobs ارسال نمی‌شود؟ چند نمونه را بررسی کن."></textarea>
<button id="send">بررسی و پاسخ</button><button type="button" id="clear">گفتگوی جدید</button></form>
<p id="status" role="status"></p><div id="answers" aria-live="polite"></div></section>
<script nonce="''' + nonce + '''">
const form=document.querySelector('#chat'), send=document.querySelector('#send'), clear=document.querySelector('#clear');
const status=document.querySelector('#status'), answers=document.querySelector('#answers'); let history=[];
clear.onclick=()=>{history=[];answers.replaceChildren();status.textContent='';form.question.value='';};
form.onsubmit=async(event)=>{event.preventDefault();const question=form.question.value.trim();if(!question)return;
send.disabled=clear.disabled=true;status.textContent='در حال بررسی دیتابیس… این کار ممکن است چند دقیقه طول بکشد.';
try{const data=new URLSearchParams({csrf:form.csrf.value,question,history:JSON.stringify(history.slice(-6))});
const response=await fetch('/assistant/ask',{method:'POST',body:data});const result=await response.json();
if(!response.ok)throw new Error(result.error||'خطا در دریافت پاسخ');
const article=document.createElement('article'), title=document.createElement('h3'), text=document.createElement('pre');
title.textContent=question;text.textContent=result.answer;article.append(title,text);
const details=document.createElement('details'), summary=document.createElement('summary'), evidence=document.createElement('pre');
summary.textContent='مسیر بررسی · '+result.provider;evidence.dir='ltr';evidence.textContent=JSON.stringify({evidence:result.evidence,providers:result.diagnostics},null,2);
details.append(summary,evidence);article.append(details);answers.prepend(article);
history.push({role:'user',content:question},{role:'assistant',content:result.answer.slice(0,8000)});history=history.slice(-6);
form.question.value='';status.textContent=result.partial ? 'تحلیل ناقص است: '+result.warning+' · شواهد و وضعیت سرویس را در مسیر بررسی ببین.' : 'بررسی تمام شد؛ پاسخ ممکن است نیاز به تأیید داشته باشد.';
}catch(error){status.textContent=error.message;}finally{send.disabled=clear.disabled=false;}};
</script></html>''', content_type='text/html')


async def assistant_ask(request):
    form = await request.post()
    try:
        raw = str(form.get('history', '[]'))
        if len(raw) > 50000:
            raise ValueError('Conversation history is too large.')
        history = json.loads(raw)
        result = await asyncio.to_thread(request.app[ANALYST_KEY].analyze, str(form.get('question', '')), history)
        return web.json_response(result)
    except ValueError as exc:
        return web.json_response({'error': str(exc)}, status=400)
    except AnalysisError as exc:
        return web.json_response({'error': str(exc)}, status=503)
    except (sqlite3.Error, OSError):
        return web.json_response({'error': 'Database could not be read. Check its path and permissions.'}, status=503)
