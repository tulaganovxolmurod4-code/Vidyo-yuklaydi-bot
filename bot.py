import os
import logging
import sqlite3
import subprocess
from aiohttp import web  # <-- Veb-server uchun qo'shildi
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp
import asyncio

# --- SOZLAMALAR ---
TOKEN = "8737473852:AAEPeZ4GFGf1HrYt3sxdXWf817C7Bf6hTDA"
ADMIN_ID = 8490356906

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
            
            # Fayl nomining faqat o'zini qirqib olamiz (callback_data 64 baytdan oshib ketmasligi uchun)
            filename_short = os.path.basename(filename)
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔴 Dumaloq video qilish", callback_data=f"round_{filename_short}")]
            ])

            await message.answer_video(
                video=video_file,
                caption="✅ Marhamat, siz so'ragan video!",
                reply_markup=keyboard
            )
            await bot.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
        else:
            await processing_msg.edit_text("❌ Videoni yuklab bo'lmadi. Havola yopiq yoki mavjud emas.")

    except Exception as e:
        logging.error(f"Xatolik: {e}")
        await processing_msg.edit_text("❌ Xatolik yuz berdi. Havolaning ochiqligiga ishonch hosil qiling.")

# --- DUMALOQ VIDEOGA AYLANTIRISH ---
@dp.callback_query(F.data.startswith("round_"))
async def make_round_video(callback: types.CallbackQuery):
    file_name = callback.data.replace("round_", "", 1)
    file_path = os.path.join("downloads", file_name)
    
    if not os.path.exists(file_path):
        await callback.answer("❌ Video fayli topilmadi yoki eskirgan!", show_alert=True)
        return

    await callback.answer("⏳ Video dumaloq formatga o'tkazilmoqda...")
    
    round_filename = file_path.replace(".mp4", "_round.mp4")
    
    # Render'da xatolik bermaydigan tozalangan FFmpeg buyrug'i
    cmd = [
        'ffmpeg', '-y', '-i', file_path,
        '-vf', 'scale=360:360:force_original_aspect_ratio=increase,crop=360:360',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-b:a', '128k',
        round_filename
    ]
    
    try:
        subprocess.run(cmd, check=Test := True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        if os.path.exists(round_filename):
            video_note = types.FSInputFile(round_filename)
            await callback.message.answer_video_note(video=video_note)
            
            if os.path.exists(file_path):
                os.remove(file_path)
            if os.path.exists(round_filename):
                os.remove(round_filename)
        else:
            await callback.message.answer("❌ Videoni dumaloq qilishda xatolik yuz berdi.")
            
    except Exception as e:
        logging.error(f"FFmpeg xatolik: {e}")
        await callback.message.answer("❌ Konvertatsiya qilishda xatolik yuz berdi.")

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
    # Veb-serverni va bot pollingni birga ishga tushiramiz
    await web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
