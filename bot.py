import asyncio
import os
import re
import subprocess
import threading
import uuid
import httpx
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile
import yt_dlp


BOT_TOKEN = os.getenv("BOT_TOKEN")
COBALT_API = os.getenv("COBALT_API")
ACCESS_KEY = "sona"
CHANNEL_ID = -1004452729797

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set.")


TMP_DIR = os.getenv("TMP_DIR", "/tmp/tg_video_bot")
os.makedirs(TMP_DIR, exist_ok=True)


MAX_MB = 49
MAX_DURATION = 300  # 5 دقیقه
MAX_HEIGHT = 1080
DEFAULT_HEIGHT = 480
MAX_DOWNLOAD_MB = 500

dp = Dispatcher()
bot = Bot(token=BOT_TOKEN)
one_download_at_a_time = asyncio.Semaphore(1)

# ذخیره کاربران احراز هویت شده در حافظه
authenticated_users = set()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "8081"))
    def run():
        try:
            server = HTTPServer(("0.0.0.0", port), HealthHandler)
            server.serve_forever()
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def parse_time(value):
    value = value.strip()
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)
    m = re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)", value)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.fullmatch(r"(\d{1,2}):(\d{1,2}(?:\.\d+)?)", value)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    raise ValueError("زمان نامعتبر. مثال: 30 یا 01:30 یا 00:01:30")


def is_youtube_url(url):
    return "youtube.com" in url or "youtu.be" in url


def make_format(height):
    return (
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]/"
        f"bestvideo*+bestaudio/best"
    )


def get_final_filepath(ydl, info):
    if info.get("requested_downloads"):
        path = info["requested_downloads"][0].get("filepath")
        if path:
            return path
    return ydl.prepare_filename(info)


def download_from_cobalt(url, height):
    if not COBALT_API:
        raise ValueError("Cobalt API تنظیم نشده")
    
    response = httpx.post(
        f"{COBALT_API}/",
        json={
            "url": url,
            "videoQuality": str(height),
            "downloadMode": "auto",
            "filenameStyle": "basic",
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=60.0
    )
    
    data = response.json()
    
    if data.get("status") == "error":
        error_info = data.get("error", {})
        error_code = error_info.get("code", "نامشخص") if isinstance(error_info, dict) else str(error_info)
        raise ValueError(f"Cobalt: {error_code}")
    
    if data.get("status") in ["redirect", "tunnel"]:
        return data["url"]
    else:
        raise ValueError(f"Cobalt: وضعیت غیرمنتظره")


def download_file(url, output_path):
    with httpx.stream("GET", url, follow_redirects=True, timeout=600.0) as r:
        r.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=8192):
                f.write(chunk)


def download_with_ytdlp(url, height):
    outtmpl = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}.%(ext)s")
    
    ydl_opts = {
        "format": make_format(height),
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": 30,
        "retries": 2,
        "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web_embedded", "tv", "default"]
            }
        }
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return get_final_filepath(ydl, info)


def get_video_duration(url):
    """مدت زمان ویدیو رو بدون دانلود گرفتن"""
    try:
        if is_youtube_url(url):
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                                    "extractor_args": {"youtube": {"player_client": ["android"]}}}) as ydl:
                info = ydl.extract_info(url, download=False)
                return info.get("duration", 0)
        else:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(url, download=False)
                return info.get("duration", 0)
    except Exception:
        return 0


def download_and_cut(url, start, end, height):
    if end <= start:
        raise ValueError("⏱ زمان پایان باید بعد از زمان شروع باشد.")
    
    duration = end - start
    if duration > MAX_DURATION:
        raise ValueError(f"⏱ حداکثر برش: {MAX_DURATION // 60} دقیقه")
    
    height = int(height)
    height = max(240, min(height, MAX_HEIGHT))
    
    input_path = None
    
    if is_youtube_url(url):
        input_path = download_with_ytdlp(url, height)
    else:
        try:
            direct_url = download_from_cobalt(url, height)
            input_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}.mp4")
            download_file(direct_url, input_path)
        except Exception as cobalt_error:
            try:
                input_path = download_with_ytdlp(url, height)
            except Exception as ytdlp_error:
                raise ValueError(
                    f"❌ دانلود ناموفق.\n"
                    f"Cobalt: {str(cobalt_error)}\n"
                    f"yt-dlp: {str(ytdlp_error)}"
                )
    
    if not input_path or not os.path.exists(input_path):
        raise RuntimeError("❌ فایل دانلود شده پیدا نشد.")
    
    # برش با FFmpeg
    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}.mp4")
    
    # تنظیمات فشرده‌سازی بر اساس مدت ویدیو
    if duration > 180:  # بیشتر از 3 دقیقه
        crf = "32"
        audio_bitrate = "64k"
    elif duration > 60:  # بیشتر از 1 دقیقه
        crf = "30"
        audio_bitrate = "80k"
    else:
        crf = "28"
        audio_bitrate = "96k"
    
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-ss", str(start),
        "-to", str(end),
        "-i", input_path,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", crf,
        "-vf", f"scale=-2:{height}",
        "-c:a", "aac",
        "-b:a", audio_bitrate,
        "-movflags", "+faststart",
        output_path,
    ]
    
    try:
        subprocess.run(cmd, check=True)
    finally:
        try:
            os.remove(input_path)
        except Exception:
            pass
    
    if not os.path.exists(output_path):
        raise RuntimeError("❌ فایل خروجی ساخته نشد")
    
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    
    if size_mb > MAX_MB:
        try:
            os.remove(output_path)
        except Exception:
            pass
        raise ValueError(
            f"⚠️ فایل {size_mb:.1f} مگابایت شد.\n"
            f"زمان کوتاه‌تر یا کیفیت پایین‌تر انتخاب کن."
        )
    
    return output_path


def download_mp3(url):
    """دانلود فقط صدا به صورت MP3"""
    outtmpl = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}.%(ext)s")
    
    if is_youtube_url(url):
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "socket_timeout": 30,
            "retries": 2,
            "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "web_embedded", "tv", "default"]
                }
            }
        }
    else:
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "socket_timeout": 30,
            "retries": 2,
            "max_filesize": MAX_DOWNLOAD_MB * 1024 * 1024,
        }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        input_path = get_final_filepath(ydl, info)
    
    if not input_path or not os.path.exists(input_path):
        raise RuntimeError("❌ فایل صوتی دانلود نشد.")
    
    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}.mp3")
    
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i", input_path,
        "-vn",
        "-c:a", "libmp3lame",
        "-q:a", "2",
        output_path,
    ]
    
    try:
        subprocess.run(cmd, check=True)
    finally:
        try:
            os.remove(input_path)
        except Exception:
            pass
    
    if not os.path.exists(output_path):
        raise RuntimeError("❌ فایل MP3 ساخته نشد")
    
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    
    if size_mb > MAX_MB:
        try:
            os.remove(output_path)
        except Exception:
            pass
        raise ValueError(f"⚠️ فایل {size_mb:.1f} مگابایت شد. ویدیوی کوتاه‌تری انتخاب کن.")
    
    return output_path


async def send_to_channel(file_path, user, is_audio=False):
    """ارسال فایل به کانال با مشخصات کاربر"""
    try:
        now = datetime.now().strftime("%Y/%m/%d %H:%M")
        
        user_info = (
            f"📌 <b>گزارش دانلود</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"👤 <b>کاربر:</b> {user.first_name or 'نامشخص'}\n"
            f"🆔 <b>آیدی:</b> <code>{user.id}</code>\n"
        )
        if user.username:
            user_info += f"🔗 <b>یوزرنیم:</b> @{user.username}\n"
        user_info += (
            f"🕐 <b>زمان:</b> {now}\n"
            f"📎 <b>نوع:</b> {'🎵 صوتی (MP3)' if is_audio else '🎬 ویدیو'}\n"
            f"━━━━━━━━━━━━━━"
        )
        
        if is_audio:
            await bot.send_audio(
                chat_id=CHANNEL_ID,
                audio=FSInputFile(file_path),
                caption=user_info,
                parse_mode="HTML"
            )
        else:
            await bot.send_video(
                chat_id=CHANNEL_ID,
                video=FSInputFile(file_path),
                caption=user_info,
                parse_mode="HTML"
            )
    except Exception as e:
        # اگه ارسال به کانال شکست خورد، خطا نده
        pass


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    
    if user_id in authenticated_users:
        await show_main_menu(message)
        return
    
    await message.answer(
        "🔐 <b>ورود به ربات</b>\n"
        "━━━━━━━━━━━━━━\n"
        "برای استفاده از ربات، کلید ورود را وارد کنید:\n\n"
        "🔑 <code>/key کلید_ورود</code>\n"
        "━━━━━━━━━━━━━━",
        parse_mode="HTML"
    )


@dp.message(Command("key"))
async def cmd_key(message: types.Message):
    user_id = message.from_user.id
    parts = message.text.split()
    
    if len(parts) < 2:
        await message.answer("❌ لطفاً کلید را وارد کنید:\n<code>/key کلید_ورود</code>", parse_mode="HTML")
        return
    
    entered_key = parts[1].strip()
    
    if entered_key == ACCESS_KEY:
        authenticated_users.add(user_id)
        await message.answer("✅ کلید صحیح است! خوش آمدید 🎉", parse_mode="HTML")
        await show_main_menu(message)
    else:
        await message.answer("❌ کلید اشتباه است. دوباره تلاش کنید.", parse_mode="HTML")


async def show_main_menu(message):
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🎬 دانلود ویدیو", callback_data="dl_video")],
        [types.InlineKeyboardButton(text="🎵 دانلود MP3", callback_data="dl_mp3")],
        [types.InlineKeyboardButton(text="📖 راهنمای استفاده", callback_data="help")],
    ])
    
    await message.answer(
        "🎬 <b>به ربات دانلود ویدیو خوش آمدید!</b>\n"
        "━━━━━━━━━━━━━━\n"
        "📥 از یوتیوب، اینستاگرام و سایت‌های دیگر\n"
        "✂️ برش ویدیو\n"
        "🎵 تبدیل به MP3\n"
        "━━━━━━━━━━━━━━\n"
        "یکی از گزینه‌ها را انتخاب کنید:",
        parse_mode="HTML",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "dl_video")
async def cb_dl_video(callback: types.CallbackQuery):
    await callback.message.answer(
        "🎬 <b>دانلود ویدیو</b>\n"
        "━━━━━━━━━━━━━━\n"
        "دستور را به این شکل بفرستید:\n\n"
        "<code>/dl لینک شروع پایان کیفیت</code>\n\n"
        "📝 <b>مثال:</b>\n"
        "<code>/dl https://youtu.be/xxxx 00:00:10 00:01:30 720</code>\n\n"
        "⏱ <b>حداکثر مدت:</b> ۵ دقیقه\n"
        "📐 <b>کیفیت:</b> 240 تا 1080\n"
        "━━━━━━━━━━━━━━",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "dl_mp3")
async def cb_dl_mp3(callback: types.CallbackQuery):
    await callback.message.answer(
        "🎵 <b>دانلود MP3</b>\n"
        "━━━━━━━━━━━━━━\n"
        "دستور را به این شکل بفرستید:\n\n"
        "<code>/mp3 لینک</code>\n\n"
        "📝 <b>مثال:</b>\n"
        "<code>/mp3 https://youtu.be/xxxx</code>\n"
        "━━━━━━━━━━━━━━",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "help")
async def cb_help(callback: types.CallbackQuery):
    await callback.message.answer(
        "📖 <b>راهنمای استفاده</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        "🎬 <b>دانلود ویدیو:</b>\n"
        "<code>/dl لینک شروع پایان کیفیت</code>\n\n"
        "🎵 <b>دانلود MP3:</b>\n"
        "<code>/mp3 لینک</code>\n\n"
        "📋 <b>نمونه‌ها:</b>\n"
        "<code>/dl https://youtu.be/xxxx 00:00:10 00:01:30 720</code>\n"
        "<code>/dl https://youtu.be/xxxx 30 90 480</code>\n"
        "<code>/mp3 https://youtu.be/xxxx</code>\n\n"
        "━━━━━━━━━━━━━━\n"
        "⏱ <b>فرمت زمان:</b>\n"
        "• ثانیه: <code>90</code>\n"
        "• دقیقه:ثانیه: <code>01:30</code>\n"
        "• ساعت:دقیقه:ثانیه: <code>00:01:30</code>\n\n"
        "📐 <b>کیفیت‌ها:</b> 240, 360, 480, 720, 1080\n\n"
        "🌐 <b>سایت‌های پشتیبانی شده:</b>\n"
        "یوتیوب، اینستاگرام، تیک‌تاک، Reddit، Vimeo و ...\n\n"
        "━━━━━━━━━━━━━━\n"
        "⚠️ <b>نکات مهم:</b>\n"
        "• حداکثر حجم فایل: ۴۹ مگابایت\n"
        "• حداکثر مدت برش: ۵ دقیقه\n"
        "• اگر فایل بزرگ شد، کیفیت یا مدت را کم کنید",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    user_id = message.from_user.id
    if user_id not in authenticated_users:
        await message.answer("🔐 ابتدا با <code>/key</code> وارد شوید.", parse_mode="HTML")
        return
    
    await cb_help_logic(message)


async def cb_help_logic(message):
    await message.answer(
        "📖 <b>راهنمای استفاده</b>\n"
        "━━━━━━━━━━━━━━\n\n"
        "🎬 <b>دانلود ویدیو:</b>\n"
        "<code>/dl لینک شروع پایان کیفیت</code>\n\n"
        "🎵 <b>دانلود MP3:</b>\n"
        "<code>/mp3 لینک</code>\n\n"
        "📋 <b>نمونه‌ها:</b>\n"
        "<code>/dl https://youtu.be/xxxx 00:00:10 00:01:30 720</code>\n"
        "<code>/dl https://youtu.be/xxxx 30 90 480</code>\n"
        "<code>/mp3 https://youtu.be/xxxx</code>\n\n"
        "━━━━━━━━━━━━━━\n"
        "⏱ <b>فرمت زمان:</b>\n"
        "• ثانیه: <code>90</code>\n"
        "• دقیقه:ثانیه: <code>01:30</code>\n"
        "• ساعت:دقیقه:ثانیه: <code>00:01:30</code>\n\n"
        "📐 <b>کیفیت‌ها:</b> 240, 360, 480, 720, 1080\n\n"
        "🌐 <b>سایت‌های پشتیبانی شده:</b>\n"
        "یوتیوب، اینستاگرام، تیک‌تاک، Reddit، Vimeo و ...\n\n"
        "━━━━━━━━━━━━━━\n"
        "⚠️ <b>نکات مهم:</b>\n"
        "• حداکثر حجم فایل: ۴۹ مگابایت\n"
        "• حداکثر مدت برش: ۵ دقیقه\n"
        "• اگر فایل بزرگ شد، کیفیت یا مدت را کم کنید",
        parse_mode="HTML"
    )


@dp.message(Command("dl"))
async def cmd_dl(message: types.Message):
    user_id = message.from_user.id
    
    if user_id not in authenticated_users:
        await message.answer("🔐 ابتدا با <code>/key</code> وارد شوید.", parse_mode="HTML")
        return
    
    parts = message.text.split()
    
    if len(parts) < 4:
        await message.answer(
            "❌ <b>دستور ناقص است!</b>\n\n"
            "📝 الگو:\n"
            "<code>/dl لینک شروع پایان کیفیت</code>\n\n"
            "📌 مثال:\n"
            "<code>/dl https://youtu.be/xxxx 00:00:10 00:01:30 720</code>",
            parse_mode="HTML"
        )
        return
    
    url = parts[1]
    
    try:
        start = parse_time(parts[2])
        end = parse_time(parts[3])
        height = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else DEFAULT_HEIGHT
    except ValueError as e:
        await message.answer(f"❌ {e}")
        return
    
    status = await message.answer(
        "⏳ <b>در حال پردازش...</b>\n"
        "📥 دانلود و برش ویدیو\n"
        "⏱ لطفاً صبر کنید...",
        parse_mode="HTML"
    )
    
    file_path = None
    
    try:
        async with one_download_at_a_time:
            file_path = await asyncio.to_thread(download_and_cut, url, start, end, height)
        
        await status.edit_text("✅ آماده شد! در حال ارسال...")
        
        # ارسال به کاربر
        await message.answer_video(
            FSInputFile(file_path),
            caption=f"🎬 برش ویدیو\n📐 کیفیت: {height}p"
        )
        
        # ارسال به کانال
        await send_to_channel(file_path, message.from_user, is_audio=False)
        
        await status.edit_text("✅ ویدیو ارسال شد!")
    
    except Exception as e:
        await status.edit_text(f"❌ خطا:\n{e}")
    
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


@dp.message(Command("mp3"))
async def cmd_mp3(message: types.Message):
    user_id = message.from_user.id
    
    if user_id not in authenticated_users:
        await message.answer("🔐 ابتدا با <code>/key</code> وارد شوید.", parse_mode="HTML")
        return
    
    parts = message.text.split()
    
    if len(parts) < 2:
        await message.answer(
            "❌ <b>دستور ناقص است!</b>\n\n"
            "📝 الگو:\n"
            "<code>/mp3 لینک</code>\n\n"
            "📌 مثال:\n"
            "<code>/mp3 https://youtu.be/xxxx</code>",
            parse_mode="HTML"
        )
        return
    
    url = parts[1]
    
    status = await message.answer(
        "⏳ <b>در حال تبدیل به MP3...</b>\n"
        "🎵 لطفاً صبر کنید...",
        parse_mode="HTML"
    )
    
    file_path = None
    
    try:
        async with one_download_at_a_time:
            file_path = await asyncio.to_thread(download_mp3, url)
        
        await status.edit_text("✅ آماده شد! در حال ارسال...")
        
        # ارسال به کاربر
        await message.answer_audio(
            FSInputFile(file_path),
            caption="🎵 دانلود MP3"
        )
        
        # ارسال به کانال
        await send_to_channel(file_path, message.from_user, is_audio=True)
        
        await status.edit_text("✅ فایل صوتی ارسال شد!")
    
    except Exception as e:
        await status.edit_text(f"❌ خطا:\n{e}")
    
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


async def main():
    start_health_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
