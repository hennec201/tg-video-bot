import asyncio
import os
import re
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import FSInputFile
import yt_dlp


BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set. Please set BOT_TOKEN environment variable.")


TMP_DIR = os.getenv("TMP_DIR", "/tmp/tg_video_bot")
os.makedirs(TMP_DIR, exist_ok=True)


# محدودیت‌ها برای شروع
MAX_MB = 49              # تلگرام معمولاً ۵۰ مگابایت را قبول می‌کند؛ ۴۹ امن‌تر است
MAX_DURATION = 60        # فعلاً فقط برش‌های حداکثر ۶۰ ثانیه
MAX_HEIGHT = 720         # حداکثر کیفیت 720p
DEFAULT_HEIGHT = 480     # کیفیت پیش‌فرض 480p
MAX_DOWNLOAD_MB = 300    # اگر فایل اصلی خیلی بزرگ باشد، دانلود را لغو می‌کند


dp = Dispatcher()
bot = Bot(token=BOT_TOKEN)

# فقط یک دانلود همزمان؛ برای هاست رایگان مهم است
one_download_at_a_time = asyncio.Semaphore(1)


# این بخش برای Railway خوب است تا سرویس را زنده تشخیص بدهد
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "8080"))

    def run():
        try:
            server = HTTPServer(("0.0.0.0", port), HealthHandler)
            server.serve_forever()
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


def parse_time(value):
    """
    زمان‌های زیر را قبول می‌کند:
    30
    90
    01:30
    00:01:30
    """
    value = value.strip()

    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)

    m = re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2}(?:\.\d+)?)", value)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    m = re.fullmatch(r"(\d{1,2}):(\d{1,2}(?:\.\d+)?)", value)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))

    raise ValueError("زمان نامعتبر است. مثال: 30 یا 00:01:30")


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


def download_and_cut(url, start, end, height):
    if end <= start:
        raise ValueError("زمان پایان باید بعد از زمان شروع باشد.")

    if end - start > MAX_DURATION:
        raise ValueError(f"برای شروع فقط برش‌های تا {MAX_DURATION} ثانیه مجاز هستند.")

    height = int(height)
    height = max(144, min(height, MAX_HEIGHT))

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
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        input_path = get_final_filepath(ydl, info)

    if not input_path or not os.path.exists(input_path):
        raise RuntimeError("فایل دانلود شده پیدا نشد.")

    output_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}.mp4")

    # برای کم‌حجم شدن، ویدیو را دوباره انکود می‌کنیم
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
        "-crf", "28",
        "-vf", f"scale=-2:{height}",
        "-c:a", "aac",
        "-b:a", "96k",
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
        raise RuntimeError("فایل خروجی ساخته نشد.")

    size_mb = os.path.getsize(output_path) / (1024 * 1024)

    if size_mb > MAX_MB:
        try:
            os.remove(output_path)
        except Exception:
            pass

        raise ValueError(
            f"فایل نهایی {size_mb:.1f} مگابایت شد. "
            f"لطفا زمان کوتاه‌تر یا کیفیت پایین‌تر انتخاب کن."
        )

    return output_path


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "سلام!\n\n"
        "برای دانلود برش ویدیو این دستور را بفرست:\n\n"
        "/dl لینک شروع پایان کیفیت\n\n"
        "مثال:\n"
        "/dl https://example.com/video 00:00:10 00:00:40 480\n\n"
        f"حداکثر برش: {MAX_DURATION} ثانیه\n"
        f"حداکثر کیفیت: {MAX_HEIGHT}p\n"
        f"حداکثر حجم ارسایی: {MAX_MB} مگابایت"
    )


@dp.message(Command("dl"))
async def cmd_dl(message: types.Message):
    parts = message.text.split()

    if len(parts) < 4:
        await message.answer(
            "کمبود دستور داری.\n\n"
            "الگو:\n"
            "/dl لینک شروع پایان کیفیت\n\n"
            "مثال:\n"
            "/dl https://example.com/video 00:00:10 00:00:40 480"
        )
        return

    url = parts[1]

    try:
        start = parse_time(parts[2])
        end = parse_time(parts[3])
        height = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else DEFAULT_HEIGHT
    except ValueError as e:
        await message.answer(f"خطا: {e}")
        return

    status = await message.answer("در حال دانلود و برش ویدیو... لطفا صبر کن.")

    file_path = None

    try:
        async with one_download_at_a_time:
            file_path = await asyncio.to_thread(download_and_cut, url, start, end, height)

        await status.edit_text("فایل آماده شد، در حال ارسال...")

        await message.answer_video(
            FSInputFile(file_path),
            caption="برش ویدیو"
        )

    except subprocess.CalledProcessError:
        await status.edit_text(
            "خطا در تبدیل/برش ویدیو. احتمالاً این لینک یا فرمت ویدیو پشتیبانی نمی‌شود."
        )

    except Exception as e:
        await status.edit_text(f"خطا:\n{e}")

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