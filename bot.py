"""MIT licensed private-by-approval media bot. Direct concurrent jobs, no queue."""
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from telegram import InputFile
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup, ReplyKeyboardRemove
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from core import LIMIT, media_limit, run_process, validate_url, ytdlp_args
from store import Store
from downloads import Downloads
import tornado.httpserver
import tornado.ioloop

load_dotenv()
LOG = logging.getLogger('social_bot')
HELP = ('أرسل الرابط ويبدأ التحميل تلقائيًا.\n'
        'للصوت: صوت ثم الرابط، أو /audio الرابط.\n'
        'اكتب اسم المقطع مباشرة للبحث في يوتيوب.\n'
        'اختر النتيجة ثم جودة الفيديو أو الصوت من الأزرار.\n'
        '/cancel يلغي تحميلاتك الجارية.\n'
        'حالات واتساب: أرسل الحالة المحفوظة كملف؛ لا يسحب البوت حالات جهات الاتصال.')


def duration_text(seconds):
    if seconds is None:
        return '—'
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError, OverflowError):
        return '—'
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes}:{seconds:02d}'


def canonical_url(value):
    u = urlsplit(value)
    if 'instagram' in (u.hostname or ''):
        return 'https://www.instagram.com' + u.path.rstrip('/') + '/'
    if u.hostname == 'youtu.be':
        return 'https://youtu.be/' + u.path.strip('/')
    if 'youtube' in (u.hostname or ''):
        from urllib.parse import parse_qs
        vid = parse_qs(u.query).get('v', [''])[0] if u.path == '/watch' else u.path.strip('/').split('/')[-1]
        if re.fullmatch(r'[\w-]{11}', vid):
            return 'https://youtu.be/' + vid
    return value


def parse_input(text):
    text = text.strip()
    if text.startswith(('بحث ', '/search ')):
        return 'search', text.split(' ', 1)[1].strip()
    audio = text.startswith(('صوت ', '/audio '))
    links = re.findall(r'https?://[^\s<>]+', text)
    if not links:
        if not text or text.startswith('/'):
            raise ValueError('اكتب اسم المقطع أو أرسل رابطًا.')
        return 'search', text[:200]
    return ('audio' if audio else 'auto'), [validate_url(v.rstrip('،.,!؛)]}')) for v in links]


class Service:
    def __init__(self, owner, store):
        self.owner = owner
        self.store = store
        self.jobs = {}  # Task -> (user ID, cancellation event); never a waiting queue.
        self.notifying = set()
        self.searches = {}
        self.previews = {}
        self.extractor_preference = {}
        base = os.getenv('PUBLIC_BASE_URL') or os.getenv('RENDER_EXTERNAL_URL') or 'http://localhost:' + os.getenv('PORT', '10000')
        self.links = Downloads(store, self.allowed, base, store.path.parent / 'downloads', max(60, int(os.getenv('LINK_TTL_SECONDS', '3600'))))
        self.http_server = None
        self.cleaner = None

    def allowed(self, uid):
        row = self.store.get(uid)
        return uid == self.owner or bool(row and row['status'] == 'approved')

    def check(self, uid, cancel):
        if cancel.is_set() or not self.allowed(uid):
            raise RuntimeError('تم إلغاء الطلب أو سحب صلاحية التحميل.')

    async def notify_owner(self, bot, row):
        uid = row['uid']
        if uid in self.notifying:
            return
        self.notifying.add(uid)
        try:
            await bot.send_message(self.owner,
                f"طلب استخدام البوت\nالاسم: {row['name']}\nالحساب: @{row['username'] or 'بدون_اسم'}\nالآيدي: {uid}",
                reply_markup=Markup([[Button('✅ موافقة', callback_data=f"access:yes:{uid}:{row['nonce']}"),
                                      Button('❌ رفض', callback_data=f"access:no:{uid}:{row['nonce']}")]]))
            self.store.mark_notified(uid)
        except TelegramError:
            LOG.warning('Owner notification unavailable; request retained for /requests')
        finally:
            self.notifying.discard(uid)

    async def gate(self, update, context, payload=None):
        if update.effective_chat.type != 'private':
            await update.effective_message.reply_text('استخدمني في الخاص.')
            return False
        uid = update.effective_user.id
        if self.allowed(uid):
            return True
        row = self.store.request(update.effective_user, payload)
        if row['status'] == 'denied':
            await update.effective_message.reply_text('ليس لديك إذن تحميل من صاحب البوت.')
        else:
            if not row['notified']:
                await self.notify_owner(context.bot, row)
            await update.effective_message.reply_text('طلبك بانتظار موافقة صاحب البوت.', reply_markup=ReplyKeyboardRemove())
        return False

    async def start(self, update, context):
        if await self.gate(update, context):
            text = HELP
            if update.effective_user.id == self.owner:
                text += '\n\nإدارة المالك:\n/requests الطلبات المعلقة\n/allow ID موافقة\n/deny ID رفض أو سحب الصلاحية'
            await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())
            if update.effective_user.id == self.owner:
                for row in self.store.pending():
                    if not row['notified']:
                        await self.notify_owner(context.bot, row)

    async def requests(self, update, context):
        if update.effective_user.id != self.owner or update.effective_chat.type != 'private':
            return
        rows = self.store.pending()
        if not rows:
            await update.message.reply_text('لا توجد طلبات معلقة.')
        for row in rows:
            await self.notify_owner(context.bot, row)

    async def callback(self, update, context):
        query = update.callback_query
        if update.effective_user.id != self.owner or update.effective_chat.type != 'private':
            await query.answer('هذه الصلاحية للمالك فقط.', show_alert=True)
            return
        _, decision, uid, nonce = query.data.split(':')
        uid = int(uid)
        row = self.store.decide(uid, nonce, 'approved' if decision == 'yes' else 'denied')
        if row is None:
            await query.answer('تم التعامل مع الطلب سابقًا أو لم يعد صالحًا.')
            return
        await query.answer('تم حفظ القرار.')
        try:
            await query.edit_message_text(('✅ تمت الموافقة على ' if decision == 'yes' else '❌ تم رفض ') + str(uid))
        except TelegramError:
            pass
        await self.deliver_decision(context.application, uid, decision == 'yes', row.get('payload'))

    async def deliver_decision(self, app, uid, approved, payload=None):
        try:
            await app.bot.send_message(uid, '✅ وافق صاحب البوت. أرسل أي رابط للتحميل مباشرة.' if approved else '❌ لم يوافق صاحب البوت على طلبك.')
        except TelegramError:
            pass
        if approved and payload:
            self.dispatch(app, uid, uid, payload)
        if not approved:
            self.cancel_user(uid)

    async def admin(self, update, context):
        if update.effective_user.id != self.owner or update.effective_chat.type != 'private':
            return
        if len(context.args) != 1 or not context.args[0].isdigit():
            await update.message.reply_text('اكتب الأمر ثم الآيدي الرقمي.')
            return
        uid = int(context.args[0])
        if uid == self.owner:
            await update.message.reply_text('صلاحية المالك ثابتة.')
            return
        approved = update.message.text.split()[0].split('@')[0] == '/allow'
        row = self.store.get(uid)
        payload = row['payload'] if row and row['status'] == 'pending' else None
        self.store.set_status(uid, 'approved' if approved else 'denied')
        await update.message.reply_text('تم حفظ الصلاحية.')
        await self.deliver_decision(context.application, uid, approved, payload)

    async def text(self, update, context):
        payload = update.message.text.strip()
        try:
            parse_input(payload)
        except ValueError as e:
            if await self.gate(update, context):
                await update.message.reply_text(str(e))
            return
        if await self.gate(update, context, payload):
            self.dispatch(context.application, update.effective_user.id, update.effective_chat.id, payload)

    def launch(self, app, uid, chat, mode, value):
        event = asyncio.Event()
        task = asyncio.create_task(self.job(app, uid, chat, mode, value, event))
        self.jobs[task] = (uid, event)
        task.add_done_callback(lambda t: self.jobs.pop(t, None))
        return task

    def dispatch(self, app, uid, chat, payload):
        if not self.allowed(uid):
            return
        try:
            mode, value = parse_input(payload)
        except ValueError:
            return
        if mode == 'search':
            self.launch(app, uid, chat, mode, value)
        else:
            for url in value:
                self.launch(app, uid, chat, 'preview' if mode == 'auto' and media_limit(url) > LIMIT else mode, url)

    def cancel_user(self, uid):
        count = 0
        for task, (user, event) in list(self.jobs.items()):
            if user == uid:
                event.set()
                count += 1
        return count

    async def cancel(self, update, context):
        if update.effective_chat.type != 'private':
            return
        count = self.cancel_user(update.effective_user.id)
        await update.message.reply_text(f'تم طلب إلغاء {count} تحميل.')

    async def media(self, update, context):
        if not await self.gate(update, context):
            return
        msg = update.message
        if msg.document:
            await msg.reply_document(msg.document.file_id)
        elif msg.video:
            await msg.reply_video(msg.video.file_id)
        elif msg.photo:
            await msg.reply_photo(msg.photo[-1].file_id, caption='للحفاظ على الجودة الأصلية أرسل الصورة كمستند من البداية.')

    async def send(self, bot, chat, kind, item, uid, cancel):
        for attempt in range(3):
            self.check(uid, cancel)
            try:
                method = getattr(bot, 'send_' + kind)
                upload = InputFile(item, filename=Path(item.name).name, read_file_handle=False) if hasattr(item, 'read') else item
                kwargs = {kind: upload, 'chat_id': chat, 'read_timeout': 900, 'write_timeout': 900}
                if kind == 'video':
                    kwargs['supports_streaming'] = True
                return await method(**kwargs)
            except RetryAfter as e:
                if attempt == 2:
                    raise
                delay = e.retry_after.total_seconds() if hasattr(e.retry_after, 'total_seconds') else e.retry_after
                if hasattr(item, 'seek'):
                    item.seek(0)
                try:
                    await asyncio.wait_for(cancel.wait(), timeout=delay + 1)
                except asyncio.TimeoutError:
                    pass

    async def job(self, app, uid, chat, mode, value, cancel):
        try:
            self.check(uid, cancel)
            started = time.monotonic()
            key = hashlib.sha256(('v5:' + mode + ':' + canonical_url(value)).encode()).hexdigest()
            cached = self.store.cached(key) if mode != 'search' else None
            if cached:
                try:
                    for kind, file_id in cached:
                        await self.send(app.bot, chat, kind, file_id, uid, cancel)
                    return
                except BadRequest:
                    self.store.uncache(key)
                    # Avoid resending a partially delivered album in the same request.
                    raise RuntimeError('تغير ملف تيليجرام المحفوظ؛ أعد إرسال الرابط لتحميل نسخة جديدة.')
            await app.bot.send_message(chat, '🔎 جارٍ البحث…' if mode == 'search' else '🎞 جارٍ تجهيز خيارات المقطع…' if mode == 'preview' else '⚡ بدأ التحميل…')
            with tempfile.TemporaryDirectory(prefix='work-', dir=self.links.root) as folder:
                cookie = ''
                configured = os.getenv('COOKIES_FILE', '')
                if configured and mode not in {'search', 'preview'} and 'instagram' in (urlsplit(value).hostname or ''):
                    cookie = str(Path(folder) / 'session.txt')
                    shutil.copyfile(configured, cookie)
                if mode == 'search':
                    await self.search_results(app.bot, uid, chat, value, folder, cancel)
                    return
                if mode == 'preview':
                    args = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist', '--skip-download', '-J', '--socket-timeout', '10', '--retries', '1', '--', value]
                    info = json.loads(await run_process(args, folder, cancel, timeout=45))
                    self.check(uid, cancel)
                    await self.preview(app.bot, uid, chat, info, value)
                    return
                download_started = time.monotonic()
                await self.download(value, mode, folder, cookie, cancel)
                LOG.warning('Timing download host=%s seconds=%.2f', urlsplit(value).hostname, time.monotonic() - download_started)
                upload_started = time.monotonic()
                self.check(uid, cancel)
                extensions = {'.mp4', '.webm', '.mkv', '.mp3', '.m4a', '.jpg', '.jpeg', '.png', '.webp', '.gif'}
                files = sorted(p for p in Path(folder).rglob('*') if p.is_file() and p.suffix.lower() in extensions)
                if not files:
                    raise RuntimeError('لم يتوفر ملف قابل للإرسال. قد يكون المحتوى خاصًا أو أكبر من حد تيليجرام أو غير متاح من السيرفر.')
                saved, skipped, linked = [], 0, 0
                for file in files:
                    self.check(uid, cancel)
                    if file.stat().st_size > media_limit(value):
                        skipped += 1
                        continue
                    if file.stat().st_size > LIMIT:
                        link = self.links.publish(file, uid)
                        linked += 1
                        await app.bot.send_message(chat, f'📥 رابط تنزيل الملف كاملًا (صالح لمدة {self.links.ttl // 60} دقيقة):\n{link}\nلا تشارك الرابط؛ من يملكه يستطيع تنزيل الملف خلال صلاحيته.', disable_web_page_preview=True)
                        continue
                    kind = 'audio' if file.suffix == '.mp3' else ('video' if file.suffix == '.mp4' else 'document')
                    try:
                        with file.open('rb') as stream:
                            result = await self.send(app.bot, chat, kind, stream, uid, cancel)
                    except BadRequest:
                        if kind == 'document':
                            raise
                        kind = 'document'
                        with file.open('rb') as stream:
                            result = await self.send(app.bot, chat, kind, stream, uid, cancel)
                    saved.append([kind, getattr(result, kind).file_id])
                LOG.warning('Timing upload seconds=%.2f total=%.2f', time.monotonic() - upload_started, time.monotonic() - started)
                if saved and not skipped and not linked:
                    self.store.cache(key, saved)
                if skipped:
                    await app.bot.send_message(chat, f'تجاوز {skipped} ملف الحد المحدد ({media_limit(value) // 1_000_000} ميجا للملف).')
        except asyncio.CancelledError:
            raise
        except Exception as e:
            LOG.warning('Request failed (%s)', type(e).__name__)
            try:
                await app.bot.send_message(chat, str(e) if isinstance(e, RuntimeError) else 'تعذر إكمال الطلب. جرّب لاحقًا.')
            except TelegramError:
                pass

    async def download(self, url, mode, folder, cookie, cancel):
        host = urlsplit(url).hostname
        gallery_first = mode != 'audio' and ('instagram' in host and '/reel' not in url or host in {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com', 'mobile.twitter.com'} or 'tiktok' in host and '/photo/' in url)
        yargs = ytdlp_args(url, 'audio' if mode == 'audio' else mode if mode.isdigit() else os.getenv('VIDEO_HEIGHT', '1080'), folder, cookie)
        gargs = [sys.executable, '-m', 'gallery_dl', '--config-ignore', '--no-input', '--retries', '3', '--http-timeout', '20', '--filesize-max', str(LIMIT), '-D', folder]
        if cookie:
            gargs += ['--cookies', cookie]
        gargs += ['--', url]
        commands = [gargs, yargs] if gallery_first else [yargs, gargs]
        if mode == 'audio' or 'youtube' in host or host == 'youtu.be':
            commands = [yargs]
        instagram = 'instagram' in host
        if instagram:
            gargs[gargs.index('--http-timeout') + 1] = '10'
            gargs[gargs.index('--retries') + 1] = '1'
        preferred = self.extractor_preference.get(host)
        if preferred and len(commands) > 1:
            commands.sort(key=lambda command: command[command.index('-m') + 1] != preferred)
        for index, args in enumerate(commands):
            attempt_started = time.monotonic()
            try:
                await run_process(args, folder, cancel,
                    timeout=int(os.getenv('INSTAGRAM_ATTEMPT_TIMEOUT', '45')) if instagram else int(os.getenv('DOWNLOAD_TIMEOUT', '0')),
                    disk_limit=int(os.getenv('JOB_DISK_LIMIT_MB', '0')) * 1_000_000)
                media_types = {'.mp4', '.webm', '.mkv', '.mp3', '.m4a', '.jpg', '.jpeg', '.png', '.webp', '.gif'}
                if not any(p.is_file() and p.suffix.lower() in media_types for p in Path(folder).rglob('*')):
                    raise RuntimeError(f'لم يتوفر ملف؛ قد يكون أكبر من الحد ({media_limit(url) // 1_000_000} ميجا) أو غير متاح.')
                self.extractor_preference[host] = args[args.index('-m') + 1]
                LOG.warning('Timing extractor=%s seconds=%.2f attempt=%s', self.extractor_preference[host], time.monotonic() - attempt_started, index + 1)
                return
            except RuntimeError as exc:
                LOG.warning('Download attempt %s/%s on %s failed: %s', index + 1, len(commands), host, exc)
                if cancel.is_set() or index == len(commands)-1:
                    raise
                # Remove partial downloads before trying the other extractor.
                for path in Path(folder).iterdir():
                    if str(path) != cookie:
                        if path.is_dir(): shutil.rmtree(path)
                        else: path.unlink()

    def trim_sessions(self):
        now = time.monotonic()
        for table in (self.searches, self.previews):
            for token, row in list(table.items()):
                if now - row['created'] > 1800:
                    del table[token]
            while len(table) > 200:
                del table[next(iter(table))]

    async def search_results(self, bot, uid, chat, term, folder, cancel):
        args = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--flat-playlist', '-J', '--socket-timeout', '10', '--retries', '1', '--', 'ytsearch20:' + term]
        raw = await run_process(args, folder, cancel, timeout=45)
        self.check(uid, cancel)
        entries = [e for e in json.loads(raw).get('entries', []) if e and re.fullmatch(r'[\w-]{11}', e.get('id', ''))]
        if not entries:
            await bot.send_message(chat, 'لا توجد نتائج. جرّب اسمًا آخر.')
            return
        self.trim_sessions()
        token = secrets.token_hex(6)
        row = dict(uid=uid, chat=chat, term=term, entries=entries, page=0, created=time.monotonic(), busy=False, exhausted=len(entries) < 20)
        self.searches[token] = row
        await bot.send_message(chat, '🔎 اختر المقطع', reply_markup=self.search_keyboard(token, row))

    def search_keyboard(self, token, row):
        start = row['page'] * 10
        keys = [[Button(f"[{duration_text(e.get('duration'))}] {e.get('title', 'مقطع')[:80]}", callback_data=f'pick:{token}:{i}')] for i, e in enumerate(row['entries'][start:start+10], start)]
        nav = [Button('✖️ إغلاق', callback_data=f'close:{token}:0')]
        if row['page']:
            nav.append(Button('◀️ السابق', callback_data=f'page:{token}:{row["page"]-1}'))
        nav.append(Button(f'{row["page"]+1}/{(len(row["entries"])+9)//10}', callback_data=f'noop:{token}:0'))
        if start + 10 < len(row['entries']) or not row['exhausted'] and len(row['entries']) < 100:
            nav.append(Button('التالي ▶️', callback_data=f'page:{token}:{row["page"]+1}'))
        return Markup(keys + [nav])

    async def preview(self, bot, uid, chat, info, url):
        self.trim_sessions()
        token = secrets.token_hex(6)
        self.previews[token] = dict(uid=uid, chat=chat, url=url, created=time.monotonic(), requested=set())
        buttons = Markup([[Button(f'🎞 {h}p', callback_data=f'dl:{token}:{h}') for h in ('480', '360', '240')], [Button('🎞 144p', callback_data=f'dl:{token}:144'), Button('🎧 صوت MP3', callback_data=f'dl:{token}:audio')]])
        caption = f"{info.get('title', 'مقطع')[:350]}\n⏱ {duration_text(info.get('duration'))}\n{info.get('uploader') or info.get('channel') or ''}\nاختر الجودة (حتى الدقة المحددة) أو الصوت:"
        thumbnail = f'https://i.ytimg.com/vi/{info.get("id", "")}/hqdefault.jpg' if re.fullmatch(r'[\w-]{11}', info.get('id', '')) else None
        if thumbnail:
            try:
                await bot.send_photo(chat, thumbnail, caption=caption, reply_markup=buttons)
                return
            except TelegramError:
                pass
        await bot.send_message(chat, caption, reply_markup=buttons)

    async def browse_callback(self, update, context):
        q = update.callback_query
        action, token, arg = q.data.split(':')
        self.trim_sessions()
        row = (self.previews if action == 'dl' else self.searches).get(token)
        uid = update.effective_user.id
        if not row or row['uid'] != uid or row['chat'] != update.effective_chat.id or not self.allowed(uid):
            await q.answer('انتهت صلاحية الأزرار؛ اكتب الاسم من جديد.', show_alert=True)
            return
        if action == 'noop':
            await q.answer()
            return
        if action == 'close':
            self.searches.pop(token, None)
            await q.answer()
            await q.edit_message_reply_markup(reply_markup=None)
            return
        if action == 'dl':
            requested = row.setdefault('requested', set())
            if arg not in {'144', '240', '360', '480', 'audio'} or arg in requested:
                await q.answer('بدأ هذا الطلب بالفعل.')
                return
            requested.add(arg)
            try:
                await q.answer('بدأ التحميل')
            except TelegramError:
                requested.discard(arg)
                raise
            task = self.launch(context.application, uid, row['chat'], arg, row['url'])
            task.add_done_callback(lambda _: requested.discard(arg))
            return
        if row['busy']:
            await q.answer('جارٍ تجهيز النتائج…')
            return
        row['busy'] = True
        cancel = asyncio.Event()
        task = asyncio.current_task()
        self.jobs[task] = (uid, cancel)
        try:
            await q.answer()
            if action == 'pick':
                i = int(arg)
                if 0 <= i < len(row['entries']):
                    info = row['entries'][i]
                    await self.preview(context.bot, uid, row['chat'], info, 'https://youtu.be/' + info['id'])
            elif action == 'page':
                page = int(arg)
                if not 0 <= page < 10:
                    return
                if page * 10 >= len(row['entries']) and not row['exhausted']:
                    count = min(100, len(row['entries']) + 20)
                    with tempfile.TemporaryDirectory(dir=self.links.root) as folder:
                        raw = await run_process([sys.executable, '-m', 'yt_dlp', '--ignore-config', '--flat-playlist', '-J', '--socket-timeout', '10', '--retries', '1', '--', f'ytsearch{count}:' + row['term']], folder, cancel, timeout=45)
                    self.check(uid, cancel)
                    entries = [e for e in json.loads(raw).get('entries', []) if e and re.fullmatch(r'[\w-]{11}', e.get('id', ''))]
                    known = {e['id'] for e in row['entries']}
                    for entry in entries:
                        if entry['id'] not in known:
                            row['entries'].append(entry)
                            known.add(entry['id'])
                    row['exhausted'] = len(entries) < count or len(row['entries']) >= 100
                if page * 10 < len(row['entries']):
                    row['page'] = page
                await q.edit_message_reply_markup(reply_markup=self.search_keyboard(token, row))
        except (RuntimeError, TelegramError, ValueError):
            await context.bot.send_message(row['chat'], 'تعذر تجهيز النتائج. جرّب البحث من جديد.')
        finally:
            self.jobs.pop(task, None)
            row['busy'] = False

    async def setup(self, app):
        self.http_server = tornado.httpserver.HTTPServer(self.links.application())
        self.http_server.listen(int(os.getenv('PORT', '10000')), address='0.0.0.0')
        self.cleaner = tornado.ioloop.PeriodicCallback(self.links.cleanup, 60_000)
        self.cleaner.start()
        await app.bot.set_my_commands([('start', 'طريقة الاستخدام'), ('audio', 'تحميل صوت'), ('search', 'بحث يوتيوب'), ('cancel', 'إلغاء تحميلاتي')])
        for row in self.store.pending():
            if not row['notified']:
                await self.notify_owner(app.bot, row)

    async def shutdown(self, app):
        if self.cleaner: self.cleaner.stop()
        if self.http_server:
            self.http_server.stop()
            await self.http_server.close_all_connections()
        tasks = list(self.jobs)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.store.close()


async def error_handler(update, context):
    LOG.error('Update failed (%s)', type(context.error).__name__)


def main():
    token = os.getenv('BOT_TOKEN', '').strip()
    owner = os.getenv('OWNER_ID', '')
    if not token or token == 'PUT_YOUR_TOKEN_HERE' or not owner.isdigit() or int(owner) <= 0:
        raise SystemExit('ضع BOT_TOKEN وOWNER_ID الصحيحين في متغيرات البيئة أو .env')
    if not shutil.which('ffmpeg') or not shutil.which('deno'):
        raise SystemExit('يلزم تثبيت FFmpeg وDeno. صورة Docker تتضمنهما.')
    data = Path(os.getenv('DATA_DIR', './data'))
    svc = Service(int(owner), Store(data / 'bot.sqlite3'))
    builder = (Application.builder().token(token).post_init(svc.setup).post_stop(svc.shutdown)
               .connection_pool_size(64).pool_timeout(60).connect_timeout(30).read_timeout(900).write_timeout(900))
    app = builder.build()
    app.add_handler(CommandHandler(['start', 'help'], svc.start))
    app.add_handler(CommandHandler('requests', svc.requests))
    app.add_handler(CommandHandler(['allow', 'deny'], svc.admin))
    app.add_handler(CommandHandler(['audio', 'search'], svc.text))
    app.add_handler(CommandHandler('cancel', svc.cancel))
    app.add_handler(CallbackQueryHandler(svc.callback, pattern=r'^access:(yes|no):\d+:[a-f0-9]{16}$'))
    app.add_handler(CallbackQueryHandler(svc.browse_callback, pattern=r'^(pick|page|close|noop|dl):[a-f0-9]{12}:(\d+|audio)$', block=False))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, svc.text))
    app.add_handler(MessageHandler(filters.VIDEO | filters.PHOTO | filters.Document.ALL, svc.media))
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=['message', 'callback_query'])


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
    main()
