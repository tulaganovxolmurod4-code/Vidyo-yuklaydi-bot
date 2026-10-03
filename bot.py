import os
import glob
import time
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
FILE_LIFETIME = 20 * 60      # yuklangan videolar 20 daqiqadan keyin o'chiriladi
ffmpeg_slots = asyncio.Semaphore(2)  # bir vaqtda 2 ta ffmpeg ishi (Render uchun)

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
    """ffmpeg ni botni qotirmasdan ishga tushiradi."""
    async with ffmpeg_slots:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0


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
            InlineKeyboardButton(text="🎞 GIF", callback_data=f"gif_{filename_short}"),
        ],
        [InlineKeyboardButton(text="🔴 Dumaloq video", callback_data=f"round_{filename_short}")],
        [
            InlineKeyboardButton(text="⏩ 2x tez", callback_data=f"fast_{filename_short}"),
            InlineKeyboardButton(text="🐢 0.5x sekin", callback_data=f"slow_{filename_short}"),
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

# --- TUGMALAR: MP3 / GIF / TEZLIK / DUMALOQ ---
@dp.callback_query(F.data.regexp(r"^(mp3|gif|fast|slow|round)_"))
async def media_action(callback: types.CallbackQuery):
    action, file_name = callback.data.split("_", 1)
    file_path = os.path.join("downloads", os.path.basename(file_name))

    if not os.path.exists(file_path):
        await callback.answer("❌ Video fayli topilmadi yoki eskirgan! Havolani qayta yuboring.", show_alert=True)
        return

    await callback.answer("⏳ Qayta ishlanmoqda...")
    base = os.path.splitext(file_path)[0]
    outputs = []

    try:
        # --- MP3 ---
        if action == "mp3":
            out = f"{base}_audio.mp3"
            outputs.append(out)
            ok = await run_ffmpeg("-i", file_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", out)
            if ok and os.path.exists(out):
                await callback.message.answer_audio(types.FSInputFile(out))
            else:
                await callback.message.answer("❌ Audio ajratib bo'lmadi (videoda ovoz bo'lmasligi mumkin).")

        # --- GIF (birinchi 10 soniya) ---
        elif action == "gif":
            out = f"{base}_gif.mp4"
            outputs.append(out)
            ok = await run_ffmpeg(
                "-i", file_path, "-t", "10", "-an",
                "-vf", "scale=480:-2", "-pix_fmt", "yuv420p",
                "-c:v", "libx264", out
            )
            if ok and os.path.exists(out):
                await callback.message.answer_animation(types.FSInputFile(out))
            else:
                await callback.message.answer("❌ GIF qilib bo'lmadi.")

        # --- TEZLIK: 2x yoki 0.5x ---
        elif action in ("fast", "slow"):
            out = f"{base}_{action}.mp4"
            outputs.append(out)
            v, a = ("0.5", "2.0") if action == "fast" else ("2.0", "0.5")
            ok = await run_ffmpeg(
                "-i", file_path,
                "-filter_complex", f"[0:v]setpts={v}*PTS[v];[0:a]atempo={a}[a]",
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", out
            )
            if not ok:  # videoda ovoz bo'lmasa
                ok = await run_ffmpeg(
                    "-i", file_path, "-an", "-vf", f"setpts={v}*PTS",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", out
                )
            if ok and os.path.exists(out):
                await callback.message.answer_video(types.FSInputFile(out))
            else:
                await callback.message.answer("❌ Tezlikni o'zgartirib bo'lmadi.")

        # --- DUMALOQ VIDEO (60 soniyadan oshsa bo'laklarga bo'linadi) ---
        elif action == "round":
            pattern = f"{base}_round_%02d.mp4"
            ok = await run_ffmpeg(
                "-i", file_path,
                "-t", str(60 * MAX_ROUND_PARTS),
                "-vf", "scale=360:360:force_original_aspect_ratio=increase,crop=360:360",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-force_key_frames", "expr:gte(t,n_forced*60)",
                "-f", "segment", "-segment_time", "60",
                "-reset_timestamps", "1",
                pattern
            )
            outputs = sorted(glob.glob(f"{base}_round_*.mp4"))
            if not ok or not outputs:
                await callback.message.answer("❌ Videoni dumaloq qilishda xatolik yuz berdi.")
            else:
                if len(outputs) > 1:
                    await callback.message.answer(f"⭕ Video {len(outputs)} ta bo'lakka bo'lindi, yuborilmoqda...")
                for p in outputs:  # ketma-ket yuboriladi
                    await callback.message.answer_video_note(video_note=types.FSInputFile(p))

    except Exception as e:
        logging.error(f"FFmpeg xatolik: {e}")
        await callback.message.answer("❌ Konvertatsiya qilishda xatolik yuz berdi.")
    finally:
        remove_files(outputs)  # asl video qoladi, 20 daqiqadan keyin o'chiriladi

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
