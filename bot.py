import os
import glob
import html
import math
import time
import uuid
import logging
import asyncpg
from aiohttp import web  # Veb-server uchun
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, ChatMemberUpdatedFilter, KICKED, MEMBER
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp
import asyncio

# --- SOZLAMALAR ---
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable topilmadi! Uni sozlab qo'ying.")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable topilmadi! Supabase havolasini qo'ying.")

ADMIN_ID = int(os.environ.get("ADMIN_ID", "8490356906"))

MAX_ROUND_PARTS = 3          # dumaloq video: 3 ta bo'lakkacha (3 x 60 s)
ROUND_SIZE = 384             # 384 = tiniq. Tezroq kerak bo'lsa 320, yanada tiniq kerak bo'lsa 480
ROUND_CRF = 23               # kichik son = sifatliroq (18-28 oralig'ida), katta = tezroq
FILE_LIFETIME = 20 * 60      # yuklangan videolar 20 daqiqadan keyin o'chiriladi
MAX_UPLOAD_MB = 20           # Telegram botlar uchun yuklab olish chegarasi
MAX_MISSED = 3               # qaytgan foydalanuvchiga ko'pi bilan nechta o'tkazib yuborilgan xabar yetkaziladi
ffmpeg_slots = asyncio.Semaphore(1)  # bittadan ishlasin: birinchi bo'lak tezroq chiqadi

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

pool: asyncpg.Pool = None  # Supabase (Postgres) ulanishlar to'plami


class Broadcast(StatesGroup):
    waiting_message = State()
    confirm = State()

# --- BAZA (SUPABASE / POSTGRES) ---
async def init_db():
    global pool
    pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0
    )
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                full_name TEXT,
                username TEXT
            )
        """)
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN NOT NULL DEFAULT FALSE")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMPTZ")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS joined_at TIMESTAMPTZ NOT NULL DEFAULT now()")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                msg_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS deliveries (
                broadcast_id INT NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY (broadcast_id, user_id)
            )
        """)
        # Supabase API orqali begona kirishni yopadi (bot to'g'ridan-to'g'ri ulangani uchun ishlayveradi)
        for t in ("users", "broadcasts", "deliveries"):
            await conn.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")


async def add_user(user_id, full_name, username):
    try:
        await pool.execute(
            "INSERT INTO users (user_id, full_name, username) VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id) DO UPDATE SET full_name = EXCLUDED.full_name, "
            "username = EXCLUDED.username, is_blocked = FALSE, blocked_at = NULL",
            user_id, full_name, username
        )
    except Exception as e:
        logging.error(f"Bazaga yozishda xato: {e}")


async def set_blocked(user_id: int, blocked: bool):
    try:
        await pool.execute(
            "UPDATE users SET is_blocked = $1, "
            "blocked_at = CASE WHEN $1 THEN now() ELSE NULL END WHERE user_id = $2",
            blocked, user_id
        )
    except Exception as e:
        logging.error(f"Bloklash holatini yozishda xato: {e}")


async def get_all_users():
    return await pool.fetch(
        "SELECT user_id, full_name, username FROM users ORDER BY joined_at, user_id"
    )


async def deliver_missed(user_id: int):
    """Foydalanuvchi qaytganda, unga yetmagan oxirgi xabarlarni yetkazadi."""
    try:
        rows = await pool.fetch(
            """
            UPDATE deliveries d SET status = 'sent'
            FROM broadcasts b
            WHERE d.broadcast_id = b.id AND d.user_id = $1 AND d.status <> 'sent'
              AND d.broadcast_id IN (
                  SELECT broadcast_id FROM deliveries
                  WHERE user_id = $1 AND status <> 'sent'
                  ORDER BY broadcast_id DESC LIMIT $2
              )
            RETURNING d.broadcast_id, b.chat_id, b.msg_id
            """,
            user_id, MAX_MISSED
        )
    except Exception as e:
        logging.error(f"Yetkazilmagan xabarlarni olishda xato: {e}")
        return

    for r in sorted(rows, key=lambda x: x["broadcast_id"]):
        try:
            await bot.copy_message(chat_id=user_id, from_chat_id=r["chat_id"], message_id=r["msg_id"])
        except Exception as e:
            logging.error(f"Yetkazilmagan xabarni yuborishda xato ({user_id}): {e}")
            try:
                await pool.execute(
                    "UPDATE deliveries SET status = 'failed' WHERE broadcast_id = $1 AND user_id = $2",
                    r["broadcast_id"], user_id
                )
            except Exception:
                pass


async def touch_user(user: types.User):
    """Foydalanuvchini bazaga yozadi/yangilaydi va unga yetmagan xabarlarni yetkazadi."""
    await add_user(user.id, user.full_name, user.username)
    await deliver_missed(user.id)


def user_line(name, username, uid) -> str:
    uname = f"@{html.escape(username)}" if username else "username yo'q"
    return f'• <a href="tg://user?id={uid}">{html.escape(name or "Noma\'lum")}</a> | {uname} | <code>{uid}</code>'

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
                # fayl vaqtining yangisini olamiz (asl video vaqti adashtirmasin)
                age_time = max(os.path.getmtime(p), os.path.getctime(p))
                if now - age_time > FILE_LIFETIME:
                    os.remove(p)
            except OSError:
                pass

# --- BOT BLOKLANGANDA / BLOKDAN CHIQARILGANDA ---
@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=KICKED))
async def on_bot_blocked(event: types.ChatMemberUpdated):
    await set_blocked(event.from_user.id, True)


@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def on_bot_unblocked(event: types.ChatMemberUpdated):
    await touch_user(event.from_user)  # blokdan chiqqan: yetmagan xabarlar yetkaziladi

# --- START BUYrug'i ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        "Assalomu alaykum! 👋\n\n"
        "Men Instagram, TikTok va YouTube videolarini yuklab beruvchi mutlaqo bepul botman.\n\n"
        "Marhamat, menga video havolasini yuboring! 📥\n\n"
        "Yoki galereyadagi videoni yuboring, uni dumaloq video yoki MP3 qilib beraman. 🔴"
    )
    await touch_user(message.from_user)

# --- ADMIN PANEL (/admin) ---
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    total_users = await pool.fetchval("SELECT COUNT(*) FROM users")
    blocked_users = await pool.fetchval("SELECT COUNT(*) FROM users WHERE is_blocked")
    recent_users = await pool.fetch(
        "SELECT user_id, full_name, username FROM users ORDER BY joined_at DESC, user_id DESC LIMIT 20"
    )

    text = "📊 <b>Bot statistikasi:</b>\n\n"
    text += f"👥 Jami foydalanuvchilar: <b>{total_users}</b> ta\n"
    text += f"✅ Faol: <b>{total_users - blocked_users}</b> ta\n"
    text += f"🚫 Botni bloklagan: <b>{blocked_users}</b> ta\n\n"
    text += "👤 <b>Oxirgi kirgan foydalanuvchilar:</b>\n"
    for u in recent_users:
        text += user_line(u["full_name"], u["username"], u["user_id"]) + "\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Hammaga xabar yuborish", callback_data="bc_start")],
        [InlineKeyboardButton(text="🚫 Bloklaganlar ro'yxati", callback_data="bl_list")],
    ])

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@dp.callback_query(F.data == "bl_list")
async def blocked_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await callback.answer()

    total = await pool.fetchval("SELECT COUNT(*) FROM users WHERE is_blocked")
    rows = await pool.fetch(
        "SELECT user_id, full_name, username FROM users WHERE is_blocked "
        "ORDER BY blocked_at DESC NULLS LAST LIMIT 40"
    )
    if not rows:
        await callback.message.answer("🚫 Botni bloklaganlar hozircha yo'q.")
        return

    text = f"🚫 <b>Botni bloklaganlar:</b> {total} ta\n\n"
    for u in rows:
        text += user_line(u["full_name"], u["username"], u["user_id"]) + "\n"
    if total > len(rows):
        text += f"\n... va yana {total - len(rows)} ta"
    await callback.message.answer(text, parse_mode="HTML")

# --- ADMIN: HAMMAGA XABAR YUBORISH (BROADCAST) ---
# Muhim: bu bloklar video/havola handlerlaridan OLDIN turishi kerak.
@dp.message(Command("cancel"))
async def cancel_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await message.answer("❌ Bekor qilindi.")


@dp.callback_query(F.data == "bc_start")
async def bc_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await state.set_state(Broadcast.waiting_message)
    await callback.message.answer(
        "📢 Barcha foydalanuvchilarga yuboriladigan xabarni yozing.\n"
        "Matn, rasm yoki video yuborishingiz mumkin.\n\n"
        "⚠️ Yuborgan xabaringizni shu chatdan o'chirib yubormang, aks holda "
        "yetmagan odamlarga keyin yetkazib bo'lmaydi.\n\n"
        "Bekor qilish: /cancel"
    )
    await callback.answer()


@dp.message(Broadcast.waiting_message)
async def bc_receive(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.update_data(chat_id=message.chat.id, msg_id=message.message_id)
    await state.set_state(Broadcast.confirm)

    total = len(await get_all_users())
    await message.answer("👆 Foydalanuvchilarga shu xabar yuboriladi (ko'rinishi shunday bo'ladi).")
    await message.answer(
        f"Jami <b>{total}</b> ta foydalanuvchiga yuborilsinmi?\n"
        f"(Avval bloklaganlarga ham urinib ko'riladi.)",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Yuborish", callback_data="bc_yes"),
            InlineKeyboardButton(text="❌ Bekor qilish", callback_data="bc_no"),
        ]])
    )


async def send_one(uid: int, from_chat: int, msg_id: int) -> str:
    """'sent' / 'blocked' / 'failed' qaytaradi."""
    for _ in range(2):
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=from_chat, message_id=msg_id)
            return "sent"
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramForbiddenError:
            return "blocked"  # foydalanuvchi botni bloklagan
        except Exception as e:
            logging.error(f"Broadcast xato ({uid}): {e}")
            return "failed"
    return "failed"


@dp.callback_query(Broadcast.confirm, F.data.in_({"bc_yes", "bc_no"}))
async def bc_confirm(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return

    data = await state.get_data()
    await state.clear()

    if callback.data == "bc_no":
        await callback.message.edit_text("❌ Bekor qilindi.")
        await callback.answer()
        return

    await callback.answer()
    await callback.message.edit_text("📤 Yuborilmoqda, kuting...")

    bid = await pool.fetchval(
        "INSERT INTO broadcasts (chat_id, msg_id) VALUES ($1, $2) RETURNING id",
        data["chat_id"], data["msg_id"]
    )

    sent = blocked = failed = 0
    unreached = []  # (user, sabab)

    for u in await get_all_users():
        uid = u["user_id"]
        status = await send_one(uid, data["chat_id"], data["msg_id"])

        if status == "sent":
            sent += 1
            await set_blocked(uid, False)
        elif status == "blocked":
            blocked += 1
            await set_blocked(uid, True)
            unreached.append((u, "bloklagan"))
        else:
            failed += 1
            unreached.append((u, "xato"))

        try:
            await pool.execute(
                "INSERT INTO deliveries (broadcast_id, user_id, status) VALUES ($1, $2, $3) "
                "ON CONFLICT (broadcast_id, user_id) DO UPDATE SET status = EXCLUDED.status",
                bid, uid, status
            )
        except Exception as e:
            logging.error(f"Yetkazish holatini yozishda xato: {e}")

        await asyncio.sleep(0.05)  # Telegram limitidan oshib ketmaslik uchun

    text = (
        "✅ <b>Xabar yuborish tugadi.</b>\n\n"
        f"📬 Yetkazildi: <b>{sent}</b> ta\n"
        f"🚫 Botni bloklagan (yetmadi): <b>{blocked}</b> ta\n"
        f"⚠️ Boshqa xato: <b>{failed}</b> ta\n"
    )
    if unreached:
        text += "\n<b>Yetib bormaganlar:</b>\n"
        for u, reason in unreached[:30]:
            text += user_line(u["full_name"], u["username"], u["user_id"]) + f" ({reason})\n"
        if len(unreached) > 30:
            text += f"... va yana {len(unreached) - 30} ta\n"
        text += "\nBular botga qaytib kelganda, xabar ularga o'zi yetkaziladi."

    await callback.message.answer(text, parse_mode="HTML")

# --- GALEREYADAN YUBORILGAN VIDEO ---
@dp.message(F.video | (F.document & F.document.mime_type.startswith("video/")))
async def handle_user_video(message: types.Message):
    user = message.from_user
    await touch_user(user)

    media = message.video or message.document

    if media.file_size and media.file_size > MAX_UPLOAD_MB * 1024 * 1024:
        await message.answer(
            f"❌ Video hajmi {MAX_UPLOAD_MB} MB dan katta. "
            f"Telegram botlarga katta fayl yuklashga ruxsat bermaydi. "
            f"Qisqaroq yoki kichikroq video yuboring."
        )
        return

    os.makedirs("downloads", exist_ok=True)
    filename_short = f"{user.id}_{uuid.uuid4().hex[:8]}.mp4"
    path = os.path.join("downloads", filename_short)

    wait_msg = await message.answer("⏳ Video qabul qilinmoqda...")
    try:
        await bot.download(media, destination=path)
        await message.answer(
            "✅ Video qabul qilindi. Nima qilay?",
            reply_markup=make_keyboard(filename_short)
        )
    except Exception as e:
        logging.error(f"Video qabul qilishda xatolik: {e}")
        await message.answer("❌ Videoni qabul qilib bo'lmadi, qayta urinib ko'ring.")
    finally:
        try:
            await wait_msg.delete()
        except Exception:
            pass

# --- VIDEO YUKLASH QISMI (havola) ---
@dp.message(F.text.regexp(r'https?://[^\s]+'))
async def download_video(message: types.Message):
    user = message.from_user
    await touch_user(user)

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
        'updatetime': False,   # fayl vaqtini videoning asl vaqtiga o'zgartirmasin
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        if os.path.exists(filename):
            os.utime(filename, None)  # fayl vaqtini hozirgiga yangilaydi
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
    await init_db()   # Supabase bazasiga ulanish va jadvallarni yaratish/yangilash
    await bot.delete_webhook(drop_pending_updates=True)
    await web_server()
    asyncio.create_task(cleanup_loop())
    try:
        await dp.start_polling(bot)
    finally:
        await pool.close()

if __name__ == "__main__":
    asyncio.run(main())
