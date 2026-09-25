import os
import logging
import sqlite3
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
import yt_dlp
import asyncio

# --- SOZLAMALAR ---
TOKEN = "8737473852:AAEPeZ4GFGf1HrYt3sxdXWf817C7Bf6hTDA"
ADMIN_ID = 8490356906  # Sizning Telegram ID raqamingiz

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
    # Faqat siz yozgandagina ishlaydi
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

    output_template = f"downloads/{message.from_user.id}_%(id)s.%(ext)s"
    os.makedirs("downloads", exist_ok=True)

    ydl_opts = {
        'outtmpl': output_template,
        'format': 'best[ext=mp4]/best',
        'noplaylist': True,
        'extractor-args': 'youtube:player_client=ios,web',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        if os.path.exists(filename):
            video_file = types.FSInputFile(filename)
            await message.answer_video(
                video=video_file,
                caption="✅ Marhamat, siz so'ragan video!"
            )
            await bot.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
            os.remove(filename)
        else:
            await processing_msg.edit_text("❌ Videoni yuklab bo'lmadi. Havola yopiq yoki mavjud emas.")

    except Exception as e:
        logging.error(f"Xatolik: {e}")
        await processing_msg.edit_text("❌ Xatolik yuz berdi. Havolaning ochiqligiga ishonch hosil qiling.")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
