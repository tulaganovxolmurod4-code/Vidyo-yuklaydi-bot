import os
import glob
import math
import time
import uuid
import logging
import sqlite3
from aiohttp import web  # Veb-server uchun
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp
import asyncio

# --- SOZLAMALAR ---
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable topilmadi! Uni sozlab qo'ying.")

ADMIN_ID = int(os.environ.get("ADMIN_ID", "8490356906"))

MAX_ROUND_PARTS = 3          # dumaloq video: 3 ta bo'lakkacha (3 x 60 s)
ROUND_SIZE = 384             # 384 = tiniq. Tezroq kerak bo'lsa 320, yanada tiniq kerak bo'lsa 480
ROUND_CRF = 23               # kichik son = sifatliroq (18-28 oralig'ida), katta = tezroq
FILE_LIFETIME = 20 * 60      # yuklangan videolar 20 daqiqadan keyin o'chiriladi
ffmpeg_slots = asyncio.Semaphore(1)  # bittadan ishlasin: birinchi bo'lak tezroq chiqadi

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- BAZANI YARATISH ---
def db_connect():
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT
        )
    """)
    conn.commit()
    conn.close()

def add_user(user_id, full_name, username):
    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, full_name, username) VALUES (?, ?, ?)",
                   (user_id, full_name, username))
    conn.commit()
    conn.close()

db_connect()

# --- YORDAMCHI FUNKSIYALAR ---
async def run_ffmpeg(*args: str) -> bool:
    """ffmpeg ni botni qotirmasdan ishga tushiradi, xatoni logga yozadi."""
    async with ffmpeg_slots:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            logging.error("FFMPEG XATO: " + err.decode(errors="ignore")[-800:])
        return proc.returncode == 0


async def get_duration(path: str) -> float:
    """Video uzunligini soniyada qaytaradi."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


async def make_round_part(src: str, out: str, start: int) -> bool:
    """Videoning 60 soniyalik bo'lagini dumaloq (kvadrat) formatga o'tkazadi."""
    s = ROUND_SIZE
    return await run_ffmpeg(
        "-ss", str(start), "-i", src, "-t", "60",
        "-vf", f"fps=30,scale={s}:{s}:force_original_aspect_ratio=increase:flags=lanczos,crop={s}:{s}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(ROUND_CRF), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-ac", "1",
        out
    )


def remove_files(paths):
    for p in paths:
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def make_keyboard(filename_short: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎵 MP3", callback_data=f"mp3_{filename_short}"),
            InlineKeyboardButton(text="🔴 Dumaloq video", callback_data=f"round_{filename_short}"),
        ],
    ])


async def cleanup_loop():
    """Eski yuklangan fayllarni vaqti-vaqti bilan o'chirib turadi."""
    while True:
        await asyncio.sleep(300)
        now = time.time()
        for p in glob.glob("downloads/*"):
            try:
                if now - os.path.getmtime(p) > FILE_LIFETIME:
                    os.remove(p)
            except OSError:
                pass

# --- START BUYrug'i ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    user = message.from_user
    add_user(user.id, user.full_name, user.username)

    await message.answer(
        "Assalomu alaykum! 👋\n\n"
        "Men Instagram, TikTok va YouTube videolarini yuklab beruvchi mutlaqo bepul botman.\n\n"
        "Marhamat, menga video havolasini yuboring! 📥"
    )

# --- ADMIN PANEL (/admin) ---
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    conn = sqlite3.connect("bot_users.db")
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]

    cursor.execute("SELECT user_id, full_name, username FROM users ORDER BY user_id DESC LIMIT 20")
    recent_users = cursor.fetchall()

    conn.close()

    text = f"📊 **Bot statistikasi:**\n\n"
    text += f"👥 Jami foydalanuvchilar: **{total_users}** ta\n\n"
    text += "👤 **Oxirgi kirgan foydalanuvchilar:**\n"

    for u in recent_users:
        uid, name, uname = u
        username_str = f"@{uname}" if uname else "Username yo'q"
        text += f"• {name} | [{username_str}](tg://user?id={uid}) (`{uid}`)\n"

    await message.answer(text, parse_mode="Markdown")

# --- VIDEO YUKLASH QISMI ---
@dp.message(F.text.regexp(r'https?://[^\s]+'))
async def download_video(message: types.Message):
    user = message.from_user
    add_user(user.id, user.full_name, user.username)

    url = message.text.strip()

    platform = "Platforma"
    if "instagram.com" in url:
        platform = "📸 Instagram"
    elif "tiktok.com" in url:
        platform = "🎵 TikTok"
    elif "youtube.com" in url or "youtu.be" in url:
        platform = "▶️ YouTube"
    else:
        await message.answer("❌ Faqat Instagram, TikTok va YouTube havolalarini qo'llab-quvvatlayman.")
        return

    processing_msg = await message.answer(f"{platform}dan video yuklab olinmoqda, kuting... ⏳")

    os.makedirs("downloads", exist_ok=True)
    output_template = f"downloads/{message.from_user.id}_%(id)s.%(ext)s"

    ydl_opts = {
        'outtmpl': output_template,
        'format': 'best[ext=mp4]/best',
        'noplaylist': True,
        'extractor-args': 'youtube:player_client=android,web',
        'user_agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
        'geo_bypass': True,
        'nocheckcertificate': True,
        'socket_timeout': 30,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        if os.path.exists(filename):
            video_file = types.FSInputFile(filename)
            filename_short = os.path.basename(filename)

            await message.answer_video(
                video=video_file,
                caption="✅ Marhamat, siz so'ragan video!",
                reply_markup=make_keyboard(filename_short)
            )
            await bot.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
        else:
            await processing_msg.edit_text("❌ Videoni yuklab bo'lmadi. Havola yopiq yoki mavjud emas.")

    except Exception as e:
        logging.error(f"Xatolik: {e}")
        await processing_msg.edit_text("❌ Xatolik yuz berdi. Havolaning ochiqligiga ishonch hosil qiling.")

# --- TUGMALAR: MP3 / DUMALOQ ---
@dp.callback_query(F.data.regexp(r"^(mp3|round)_"))
async def media_action(callback: types.CallbackQuery):
    action, file_name = callback.data.split("_", 1)
    file_path = os.path.join("downloads", os.path.basename(file_name))

    if not os.path.exists(file_path):
        await callback.answer("❌ Video fayli topilmadi yoki eskirgan! Havolani qayta yuboring.", show_alert=True)
        return

    await callback.answer()  # tepadagi yozuvsiz, faqat tugma "aylanishi"ni to'xtatadi

    # Pastda (chatda) "kuting" xabari chiqadi
    wait_text = ("⏳ Iltimos kuting, dumaloq video qilinmoqda..."
                 if action == "round" else "⏳ Iltimos kuting, MP3 tayyorlanmoqda...")
    wait_msg = await callback.message.answer(wait_text)

    base = os.path.splitext(file_path)[0]
    uid = uuid.uuid4().hex[:6]   # har bir ish uchun noyob nom (to'qnashuv bo'lmasin)
    outputs = []

    try:
        # --- MP3 ---
        if action == "mp3":
            out = f"{base}_{uid}.mp3"
            outputs.append(out)
            ok = await run_ffmpeg("-i", file_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", out)
            if ok and os.path.exists(out):
                await callback.message.answer_audio(types.FSInputFile(out))
            else:
                await callback.message.answer("❌ Audio ajratib bo'lmadi (videoda ovoz bo'lmasligi mumkin).")

        # --- DUMALOQ VIDEO (60 soniyadan oshsa bo'laklarga bo'linadi) ---
        elif action == "round":
            duration = await get_duration(file_path)
            parts = max(1, min(MAX_ROUND_PARTS, math.ceil(duration / 60))) if duration else 1
            outs = [f"{base}_{uid}_r{i}.mp4" for i in range(parts)]
            outputs.extend(outs)

            # bo'laklar navbat bilan tayyorlanadi, tayyor bo'lgani darrov yuboriladi
            tasks = [asyncio.create_task(make_round_part(file_path, outs[i], i * 60))
                     for i in range(parts)]

            for i, task in enumerate(tasks):
                ok = await task
                if ok and os.path.exists(outs[i]) and os.path.getsize(outs[i]) > 1000:
                    await callback.message.answer_video_note(video_note=types.FSInputFile(outs[i]))
                else:
                    await callback.message.answer("❌ Videoni dumaloq qilishda xatolik yuz berdi.")
                    break

    except Exception as e:
        logging.error(f"FFmpeg xatolik: {e}")
        await callback.message.answer("❌ Konvertatsiya qilishda xatolik yuz berdi.")
    finally:
        remove_files(outputs)  # asl video qoladi, 20 daqiqadan keyin o'chiriladi
        try:
            await wait_msg.delete()  # "kuting" xabarini o'chiradi
        except Exception:
            pass

# --- RENDER UCHUN PORT OCHUVCHI SERVER ---
async def handle(request):
    return web.Response(text="Bot is running!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await web_server()
    asyncio.create_task(cleanup_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
