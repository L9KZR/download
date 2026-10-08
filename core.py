import asyncio
import os
import re
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
         'youtu.be', 'instagram.com', 'www.instagram.com', 'tiktok.com',
         'www.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com', 'm.tiktok.com',
         'twitter.com', 'www.twitter.com', 'mobile.twitter.com', 'x.com', 'www.x.com'}
LIMIT = 49_000_000
YOUTUBE_LIMIT = 300_000_000


def media_limit(url):
    host = urlsplit(url).hostname or ""
    return YOUTUBE_LIMIT if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"} else LIMIT


def validate_url(value):
    u = urlsplit(value.strip())
    if u.scheme not in {'http', 'https'} or u.hostname not in HOSTS:
        raise ValueError('أرسل رابطًا مباشرًا من يوتيوب أو إنستغرام أو تيك توك أو تويتر.')
    if u.username or u.password or u.port not in (None, 80, 443):
        raise ValueError('الرابط غير مقبول.')
    # Only individual content, never entire profiles or feeds.
    p = u.path
    host = u.hostname
    valid = False
    if 'youtube' in host:
        valid = p == '/watch' or p.startswith(('/shorts/', '/live/'))
    elif host == 'youtu.be':
        valid = bool(p.strip('/'))
    elif 'instagram' in host:
        valid = bool(re.match(r'^/(p|reel|reels|tv|stories)/[^/]+', p))
    elif 'tiktok' in host:
        valid = host in {'vm.tiktok.com', 'vt.tiktok.com'} or bool(re.search(r'/(video|photo)/\d+', p)) or p.startswith('/t/')
    else:
        valid = bool(re.search(r'/status/\d+', p))
    if not valid:
        raise ValueError('أرسل رابط المنشور أو المقطع نفسه، وليس رابط الحساب.')
    return value.strip()


def ytdlp_args(url, mode, folder, cookie=''):
    args = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-progress',
            '--no-playlist', '--socket-timeout', '20',
            '--retries', '3', '--fragment-retries', '3', '--concurrent-fragments', '2',
            '--max-filesize', str(media_limit(url)), '--match-filters', '!is_live',
            '-o', str(Path(folder) / '%(playlist_index)03d_%(id)s.%(ext)s')]
    if cookie:
        args += ['--cookies', cookie]
    if mode == 'audio':
        args += ['-f', 'bestaudio/best', '-x', '--audio-format', 'mp3', '--audio-quality', '192K']
    else:
        height = int(mode)
        args += ['-f', f'bv*[height<={height}][ext=mp4]+ba[ext=m4a]/b[height<={height}][ext=mp4]/b[height<={height}]',
                 '--merge-output-format', 'mp4']
    return args + ['--', url]


async def run_process(args, folder, cancel, timeout=600, disk_limit=350_000_000):
    """Bound wall time and disk; terminate the subprocess tree on cancellation."""
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=(os.name != 'nt'))
    output = bytearray()

    async def drain():
        while chunk := await proc.stdout.read(8192):
            if len(output) < 2_000_000:
                output.extend(chunk[:2_000_000-len(output)])

    reader = asyncio.create_task(drain())
    started = time.monotonic()
    try:
        while proc.returncode is None:
            if cancel.is_set():
                raise RuntimeError('تم إلغاء الطلب.')
            if timeout and time.monotonic() - started > timeout:
                raise RuntimeError('انتهت مهلة التحميل. جرّب مقطعًا أقصر أو أعد المحاولة لاحقًا.')
            size = 0
            for file in Path(folder).rglob('*'):
                try:
                    if file.is_file():
                        size += file.stat().st_size
                except FileNotFoundError:
                    pass
            if disk_limit and size > disk_limit:
                raise RuntimeError('المحتوى أكبر من الحد المتاح لهذا البوت.')
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
        await reader
        if proc.returncode:
            raise RuntimeError('تعذر جلب المحتوى: قد يكون خاصًا، محذوفًا، أو يحتاج تسجيل دخول أو تحديث المحمّل.')
        return output.decode('utf-8', errors='replace')
    finally:
        if proc.returncode is None:
            if os.name == 'nt':
                killer = await asyncio.create_subprocess_exec('taskkill', '/F', '/T', '/PID', str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await killer.wait()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await proc.wait()
        await reader
