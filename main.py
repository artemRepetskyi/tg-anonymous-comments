import os
import asyncio
import time
import hmac
import hashlib
import re
from urllib.parse import parse_qsl
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from aiogram.filters import Command, CommandObject

# ---- CONFIGURATION ----
load_dotenv() # Загружаем переменные из файла .env

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://127.0.0.1:8000/static/index.html")
# Ссылка на само приложение в Telegram (полученная через BotFather -> /newapp)
TME_APP_LINK = os.getenv("TME_APP_LINK", "https://t.me/YOUR_BOT/YOUR_APP")
# Твой ID администратора для модерации комментариев
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# ---- DATABASE (SQLite) ----
SQLALCHEMY_DATABASE_URL = "sqlite:///./database.db"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Post(Base):
    __tablename__ = "posts"
    id = Column(Integer, primary_key=True, index=True)
    telegram_message_id = Column(Integer, nullable=True)
    channel_id = Column(String, nullable=True)
    bot_message_id = Column(Integer, nullable=True)

class Comment(Base):
    __tablename__ = "comments"
    id = Column(Integer, primary_key=True, index=True)
    post_id = Column(Integer, ForeignKey("posts.id"))
    author_id = Column(Integer, index=True, nullable=True)
    author_name = Column(String, nullable=True)
    text = Column(Text, nullable=False)
    reply_to_id = Column(Integer, nullable=True)
    reply_to_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class BannedUser(Base):
    __tablename__ = "banned_users"
    id = Column(Integer, primary_key=True, index=True)
    author_id = Column(Integer, unique=True, index=True)

class CommentLike(Base):
    __tablename__ = "comment_likes"
    id = Column(Integer, primary_key=True, index=True)
    comment_id = Column(Integer, ForeignKey("comments.id"), index=True)
    user_id = Column(Integer, index=True)


Base.metadata.create_all(bind=engine)

import json

def get_telegram_user(init_data: str, token: str) -> dict | None:
    if not init_data: return None
    parsed_data = dict(parse_qsl(init_data))
    if "hash" not in parsed_data: return None
    
    hash_value = parsed_data.pop("hash")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
    
    secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    
    if calculated_hash == hash_value:
        user_str = parsed_data.get("user")
        if user_str:
            try:
                return json.loads(user_str)
            except Exception:
                pass
    return None

def verify_telegram_data(init_data: str, token: str) -> bool:
    return get_telegram_user(init_data, token) is not None

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---- FASTAPI ----
app = FastAPI(title="Anonymous Telegram Comments")

# Разрешаем CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Схемы для API
class CommentCreate(BaseModel):
    post_id: int
    text: str
    author_name: str | None = None
    author_id: int | None = None
    reply_to_id: int | None = None
    reply_to_name: str | None = None

class CommentResponse(BaseModel):
    id: int
    post_id: int
    author_id: int | None = None
    author_name: str | None = None
    text: str
    reply_to_id: int | None = None
    reply_to_name: str | None = None
    created_at: datetime
    likes_count: int = 0
    is_liked_by_me: bool = False
    
    model_config = {"from_attributes": True} # pydantic v2

class BanRequest(BaseModel):
    comment_id: int
    admin_id: int

class DeleteRequest(BaseModel):
    comment_id: int
    user_id: int

@app.get("/config")
def get_config():
    return {"admin_id": ADMIN_ID}

@app.get("/comments/{post_id}", response_model=list[CommentResponse])
def get_comments(
    post_id: int, 
    db: Session = Depends(get_db),
    x_telegram_init_data: str | None = Header(None)
):
    comments = db.query(Comment).filter(Comment.post_id == post_id).order_by(Comment.created_at).all()
    user = get_telegram_user(x_telegram_init_data, BOT_TOKEN) if x_telegram_init_data else None
    user_id = user.get("id") if user else None
    
    result = []
    for c in comments:
        likes_count = db.query(CommentLike).filter(CommentLike.comment_id == c.id).count()
        is_liked = False
        if user_id:
            is_liked = db.query(CommentLike).filter(CommentLike.comment_id == c.id, CommentLike.user_id == user_id).first() is not None
            
        c_dict = {
            "id": c.id, "post_id": c.post_id, "author_id": c.author_id, "author_name": c.author_name,
            "text": c.text, "reply_to_id": c.reply_to_id, "reply_to_name": c.reply_to_name,
            "created_at": c.created_at, "likes_count": likes_count, "is_liked_by_me": is_liked
        }
        result.append(c_dict)
    return result

@app.post("/comments/{comment_id}/like")
def toggle_like(
    comment_id: int, 
    db: Session = Depends(get_db),
    x_telegram_init_data: str | None = Header(None)
):
    user = get_telegram_user(x_telegram_init_data, BOT_TOKEN)
    if not user:
        raise HTTPException(status_code=403, detail="Доступ запрещен. Запрос не из Telegram.")
    
    user_id = user.get("id")
    comment = db.query(Comment).filter(Comment.id == comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Комментарий не найден.")
        
    existing_like = db.query(CommentLike).filter(CommentLike.comment_id == comment_id, CommentLike.user_id == user_id).first()
    if existing_like:
        db.delete(existing_like)
        db.commit()
        liked = False
    else:
        new_like = CommentLike(comment_id=comment_id, user_id=user_id)
        db.add(new_like)
        db.commit()
        liked = True
        
    likes_count = db.query(CommentLike).filter(CommentLike.comment_id == comment_id).count()
    return {"liked": liked, "likes_count": likes_count}

# Хранилище для анти-спама
RATE_LIMIT_STORE = {}

# ---- AIOGRAM BOT ----
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

async def update_post_button(post_id: int, db: Session):
    try:
        post = db.query(Post).filter(Post.id == post_id).first()
        if not post or not post.channel_id or not post.bot_message_id:
            return
            
        count = db.query(Comment).filter(Comment.post_id == post_id).count()
        text = f"💬 Прокомментировать ({count})" if count > 0 else "💬 Прокомментировать"
        
        btn = InlineKeyboardButton(text=text, url=f"{TME_APP_LINK}?startapp={post.id}")
        markup = InlineKeyboardMarkup(inline_keyboard=[[btn]])
        
        await bot.edit_message_reply_markup(chat_id=post.channel_id, message_id=post.bot_message_id, reply_markup=markup)
    except Exception as e:
        print(f"Не удалось обновить счетчик на кнопке: {e}")

async def notify_admin_about_comment(comment_id: int, post_id: int, author_id: int, author_name: str, text: str):
    if not ADMIN_ID or ADMIN_ID == 0:
        return
    try:
        # Кнопки в один ряд, короткие названия (только иконки + суть)
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Удалить", callback_data=f"del_{comment_id}"),
            InlineKeyboardButton(text="🚫 Бан", callback_data=f"ban_{comment_id}"),
            InlineKeyboardButton(text="👀 К посту", url=f"{TME_APP_LINK}?startapp={post_id}")
        ]])
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=f"💬 <b>Новый комментарий</b>\nОт: <a href='tg://user?id={author_id}'>{author_name}</a>\n\n{text}",
            parse_mode="HTML",
            reply_markup=markup
        )
    except Exception as e:
        print(f"Не удалось отправить уведомление админу: {e}")

@app.post("/comments", response_model=CommentResponse)
async def create_comment(
    comment: CommentCreate, 
    db: Session = Depends(get_db),
    x_telegram_init_data: str | None = Header(None)
):
    tg_user = get_telegram_user(x_telegram_init_data, BOT_TOKEN)
    if not tg_user:
        raise HTTPException(status_code=403, detail="Доступ запрещен. Запрос не из Telegram.")

    # Защита от спама и лимиты
    if comment.author_id:
        if db.query(BannedUser).filter(BannedUser.author_id == comment.author_id).first():
            raise HTTPException(status_code=403, detail="Вы были заблокированы администратором.")
        
        # Защита от спама (1 сообщение в 5 секунд)
        if comment.author_id != ADMIN_ID:
            last_time = RATE_LIMIT_STORE.get(comment.author_id, 0)
            if time.time() - last_time < 5:
                raise HTTPException(status_code=429, detail="Подождите 5 секунд.")
            RATE_LIMIT_STORE[comment.author_id] = time.time()
            
            # Проверка на ссылки (анти-спам URLs и контакты)
            link_pattern = re.compile(
                r'(https?://|www\.|t\.me/|telegram\.me/|@[\w\d_]+|[\w.-]+@[\w.-]+\.\w+|\b[a-zA-Z0-9.-]+\.[a-zA-Z]{2,6}\b(?:/[^\s]*)?)', 
                re.IGNORECASE
            )
            if link_pattern.search(comment.text):
                raise HTTPException(
                    status_code=400, 
                    detail="В этом чате запрещено отправлять ссылки."
                )
            
            # Лимит длины комментария (максимум 400 символов)
            if len(comment.text) > 400:
                raise HTTPException(status_code=400, detail="Комментарий слишком длинный (максимум 400 символов).")

    # Проверка, существует ли пост
    post = db.query(Post).filter(Post.id == comment.post_id).first()
    if not post:
        new_post = Post(id=comment.post_id, telegram_message_id=comment.post_id)
        db.add(new_post)
        db.commit()
        db.refresh(new_post)
        
    first_name = tg_user.get("first_name", "Аноним")
    last_name = tg_user.get("last_name", "")
    real_name = f"{first_name} {last_name}".strip()
        
    new_comment = Comment(
        post_id=comment.post_id, 
        author_id=comment.author_id, 
        author_name=real_name, 
        text=comment.text,
        reply_to_id=comment.reply_to_id,
        reply_to_name=comment.reply_to_name
    )
    db.add(new_comment)
    db.commit()
    db.refresh(new_comment)
    
    # Обновляем кнопку через отдельную таску (чтобы не тормозить ответ пользователю)
    asyncio.create_task(update_post_button(comment.post_id, db))
    
    if comment.author_id != ADMIN_ID:
        asyncio.create_task(notify_admin_about_comment(new_comment.id, comment.post_id, comment.author_id, real_name, comment.text))
        
    return new_comment

@app.post("/delete")
async def delete_comment(
    req: DeleteRequest, 
    db: Session = Depends(get_db),
    x_telegram_init_data: str | None = Header(None)
):
    tg_user = get_telegram_user(x_telegram_init_data, BOT_TOKEN)
    if not tg_user:
        raise HTTPException(status_code=403, detail="Ошибка безопасности. Запрос не из Telegram.")
        
    comment = db.query(Comment).filter(Comment.id == req.comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Комментарий не найден.")
        
    if comment.author_id != tg_user.get("id") and tg_user.get("id") != ADMIN_ID:
        raise HTTPException(status_code=403, detail="Вы не можете удалить чужой комментарий.")
        
    post_id = comment.post_id
    db.delete(comment)
    db.commit()
    
    # Обновляем интерфейс в фоне
    asyncio.create_task(update_post_button(post_id, db))
    
    return {"status": "ok", "message": "Комментарий удален."}

@app.post("/ban")
async def ban_user(
    req: BanRequest, 
    db: Session = Depends(get_db),
    x_telegram_init_data: str | None = Header(None)
):
    # --- КРИПТОГРАФИЧЕСКАЯ ЗАЩИТА ТЕЛЕГРАМ ---
    # Проверяем, что запрос реально отправлен из официального клиента Telegram
    if not x_telegram_init_data or not verify_telegram_data(x_telegram_init_data, BOT_TOKEN):
        raise HTTPException(status_code=403, detail="Ошибка безопасности. Поддельный запрос от хакера.")

    if ADMIN_ID == 0 or req.admin_id != ADMIN_ID:
        raise HTTPException(status_code=403, detail="У вас нет прав администратора.")
        
    comment = db.query(Comment).filter(Comment.id == req.comment_id).first()
    if not comment:
        raise HTTPException(status_code=404, detail="Комментарий не найден.")
        
    post_id = comment.post_id
    
    if comment.author_id:
        # Добавляем в бан
        if not db.query(BannedUser).filter(BannedUser.author_id == comment.author_id).first():
            db.add(BannedUser(author_id=comment.author_id))
        
        # Удаляем все его комментарии
        db.query(Comment).filter(Comment.author_id == comment.author_id).delete()
        
    db.commit()
    
    # Обновляем интерфейс в фоне
    asyncio.create_task(update_post_button(post_id, db))
    
    return {"status": "ok", "message": "Пользователь забанен."}

# ---- AIOGRAM BOT ----
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет!\n"
        "Узнать свой ID для админки — жми /myid\n\n"
        "Как настроить систему комментариев:\n"
        "1. Создайте группу (или используйте существующую) и привяжите её к вашему каналу как 'Обсуждение'.\n"
        "2. Добавьте меня в эту группу и дайте право 'Отправка сообщений'.\n"
        "3. В настройках группы запретите обычным пользователям писать сообщения.\n"
        "Готово! Теперь я буду ловить все новые посты в этой группе и выдавать под ними красивую кнопку для WebApp-комментариев."
    )

@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    await message.answer(
        f"Твой Telegram ID: `{message.from_user.id}`\n\n"
        f"Скопируй это число и вставь его в файл `.env` напротив `ADMIN_ID=...`, чтобы получить возможность банить комментаторов прямо из приложения.",
        parse_mode="Markdown"
    )

@dp.message(Command("link"))
async def generate_link(message: types.Message): # Changed Message to types.Message
    """
    Вариант 1 (Ручной): Вы просите ссылку, бот дает её вам, вы вставляете её в текст поста.
    """
    db = SessionLocal()
    new_post = Post()
    db.add(new_post)
    db.commit()
    db.refresh(new_post)
    post_id = new_post.id
    db.close()
    
    await message.answer(
        f"✅ Место для комментариев создано!\n"
        f"Для локального тестирования в браузере откройте:\n"
        f"{WEBAPP_URL}?startapp={post_id}\n\n"
        f"Для публикации в Telegram вставьте эту ссылку в текст поста:\n"
        f"`{TME_APP_LINK}?startapp={post_id}`\n"
    )

@dp.message(F.is_automatic_forward)
async def auto_forward_add_button(message: types.Message):
    """
    Бот ловит автоматический репост из канала в привязанную группу
    и сразу же отвечает на него кнопкой для WebApp-комментариев.
    Таким образом, сам канал остается нетронутым и защищенным.
    """
    group_id = str(message.chat.id)
    thread_msg_id = message.message_id
    
    # ID автопересланного сообщения становится уникальным ID поста для WebApp
    post_id = thread_msg_id
    
    db = SessionLocal()
    try:
        post = db.query(Post).filter(Post.id == post_id).first()
        if not post:
            new_post = Post(id=post_id, telegram_message_id=thread_msg_id, channel_id=group_id)
            db.add(new_post)
            db.commit()
            db.refresh(new_post)
            post_to_update = new_post
        else:
            post.channel_id = group_id
            db.commit()
            post_to_update = post
            
        btn = InlineKeyboardButton(
            text="💬 Прокомментировать",
            url=f"{TME_APP_LINK}?startapp={post_id}"
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[[btn]])
        
        try:
            # Бот пишет сообщение-ответ прямо в группе-комментариях
            sent_msg = await bot.send_message(
                chat_id=group_id, 
                text="👇 Оставьте комментарий к этому посту:",
                reply_to_message_id=thread_msg_id,
                reply_markup=markup
            )
            post_to_update.bot_message_id = sent_msg.message_id
            db.commit()
        except Exception as e:
            print(f"Ошибка при ответе в группу: {e}")
            
    finally:
        db.close()

@dp.message(Command("bans"))
async def cmd_bans(message: types.Message):
    """Список забаненных"""
    if message.from_user.id != ADMIN_ID:
        return
        
    db = SessionLocal()
    try:
        banned = db.query(BannedUser).all()
        if not banned:
            await message.answer("Список забаненных пуст.")
            return
            
        text = "🚫 Забаненные пользователи (их ID):\n"
        for user in banned:
            text += f"`{user.author_id}`\n"
        text += "\nЧтобы разбанить, отправьте: `/unban ID`"
        await message.answer(text, parse_mode="Markdown")
    finally:
        db.close()

@dp.message(Command("unban"))
async def cmd_unban(message: types.Message, command: CommandObject):
    """Разбан по ID"""
    if message.from_user.id != ADMIN_ID:
        return
        
    if not command.args:
        await message.answer("Укажите ID пользователя. Пример: `/unban 123456789`", parse_mode="Markdown")
        return
        
    try:
        user_id = int(command.args)
    except ValueError:
        await message.answer("ID должен быть числом.")
        return
        
    db = SessionLocal()
    try:
        banned_user = db.query(BannedUser).filter(BannedUser.author_id == user_id).first()
        if banned_user:
            db.delete(banned_user)
            db.commit()
            await message.answer(f"✅ Пользователь `{user_id}` был успешно РАЗБЛОКИРОВАН.", parse_mode="Markdown")
        else:
            await message.answer(f"Пользователь `{user_id}` не найден в списке забаненных.", parse_mode="Markdown")
    finally:
        db.close()

@dp.message(Command("disable_all"))
async def cmd_disable_all(message: types.Message):
    """Снять все кнопки отовсюду"""
    if message.from_user.id != ADMIN_ID:
        return
        
    await message.answer("Удаляю кнопки со всех известных мне постов...")
    db = SessionLocal()
    try:
        posts = db.query(Post).all()
        count = 0
        for p in posts:
            if p.channel_id and p.bot_message_id:
                try:
                    await bot.delete_message(chat_id=p.channel_id, message_id=p.bot_message_id)
                    p.bot_message_id = None
                    count += 1
                except Exception:
                    pass
        db.commit()
        await message.answer(f"✅ Успешно удалено сообщений с кнопками: {count}. Комментирование везде остановлено.")
    finally:
        db.close()

@dp.message(Command("sync_counters"))
async def cmd_sync_counters(message: types.Message):
    """Синхронизировать счетчики кнопок с реальными данными из базы"""
    if message.from_user.id != ADMIN_ID:
        return

    await message.answer("🔄 Обновляю счетчики на всех кнопках...")
    db = SessionLocal()
    updated = 0
    failed = 0
    try:
        posts = db.query(Post).all()
        for p in posts:
            if p.channel_id and p.bot_message_id:
                try:
                    await update_post_button(p.id, db)
                    updated += 1
                except Exception:
                    failed += 1
        await message.answer(
            f"✅ Синхронизация завершена!\n"
            f"Обновлено кнопок: {updated}\n"
            f"Ошибок: {failed}"
        )
    finally:
        db.close()

@dp.message(Command("clear_comments"))
async def cmd_clear_comments(message: types.Message):
    """Очистить ВСЕ комментарии, но сохранить посты и обновить кнопки"""
    if message.from_user.id != ADMIN_ID:
        return

    await message.answer("🗑 Удаляю все комментарии и баны...")
    db = SessionLocal()
    try:
        deleted = db.query(Comment).delete()
        db.query(BannedUser).delete()
        db.commit()
        
        # После очистки сразу обновляем счетчики на всех кнопках в Telegram
        posts = db.query(Post).all()
        updated = 0
        for p in posts:
            if p.channel_id and p.bot_message_id:
                try:
                    await update_post_button(p.id, db)
                    updated += 1
                except Exception:
                    pass

        await message.answer(
            f"✅ Готово!\n"
            f"Удалено комментариев: {deleted}\n"
            f"Обновлено кнопок в Telegram: {updated}\n\n"
            f"Теперь все кнопки показывают актуальный счётчик."
        )
    finally:
        db.close()


from contextlib import asynccontextmanager

bot_task = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    default_commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="myid", description="Узнать свой ID (для админки)"),
    ]
    admin_commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="bans", description="📋 Список забаненных"),
        BotCommand(command="unban", description="🔓 Разбанить (нужен ID)"),
        BotCommand(command="disable_all", description="🛑 Отключить комментарии ВЕЗДЕ"),
        BotCommand(command="sync_counters", description="🔄 Синхронизировать счетчики кнопок"),
        BotCommand(command="clear_comments", description="🗑 Очистить все комментарии"),
        BotCommand(command="link", description="🔗 Получить ручную ссылку"),
        BotCommand(command="myid", description="Узнать свой ID"),
    ]
    try:
        await bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())
        if ADMIN_ID != 0:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    except Exception as e:
        print(f"Не удалось обновить меню команд: {e}")

    global bot_task
    bot_task = asyncio.create_task(dp.start_polling(bot))
    
    yield # Сервер работает в этот момент
    
    # --- SHUTDOWN ---
    if bot_task:
        bot_task.cancel()
    await bot.session.close()

# Применяем lifespan к нашему приложению
app.router.lifespan_context = lifespan

@dp.callback_query(F.data.startswith("del_"))
async def cb_delete_comment(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    comment_id = int(callback.data.split("_")[1])
    db = SessionLocal()
    try:
        comment = db.query(Comment).filter(Comment.id == comment_id).first()
        if comment:
            post_id = comment.post_id
            db.delete(comment)
            db.commit()
            asyncio.create_task(update_post_button(post_id, db))
            await callback.message.delete()
        else:
            await callback.answer("Комментарий уже не существует.", show_alert=True)
            await callback.message.delete()
    finally:
        db.close()
    await callback.answer()

@dp.callback_query(F.data.startswith("ban_"))
async def cb_ban_user(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    comment_id = int(callback.data.split("_")[1])
    db = SessionLocal()
    try:
        comment = db.query(Comment).filter(Comment.id == comment_id).first()
        if comment:
            post_id = comment.post_id
            if comment.author_id:
                if not db.query(BannedUser).filter(BannedUser.author_id == comment.author_id).first():
                    db.add(BannedUser(author_id=comment.author_id))
                db.query(Comment).filter(Comment.author_id == comment.author_id).delete()
            db.commit()
            asyncio.create_task(update_post_button(post_id, db))
            await callback.message.delete()
        else:
            await callback.answer("Комментарий уже не существует.", show_alert=True)
            await callback.message.delete()
    finally:
        db.close()
    await callback.answer()

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.getenv("SERVER_PORT", 8000))
    
    try:
        from pyngrok import ngrok
        import time
        # Берем токен из .env (нужно будет добавить NGROK_AUTHTOKEN)
        ngrok_auth = os.getenv("NGROK_AUTHTOKEN")
        if ngrok_auth:
            ngrok.set_auth_token(ngrok_auth)
            
        time.sleep(2) # Даем время порту
        tunnel = ngrok.connect(port)
        public_url = tunnel.public_url.replace("http://", "https://")
        
        print("\n" + "="*70)
        print("  🟢 ВАШ БОТ УСПЕШНО ЗАПУЩЕН НА СЕРВЕРЕ! 🟢")
        print("="*70)
        print("👇 СКОПИРУЙТЕ ЭТУ ССЫЛКУ И ВСТАВЬТЕ В .env В ПОЛЕ WEBAPP_URL 👇")
        print(f"WEBAPP_URL={public_url}/static/index.html")
        print("="*70 + "\n")
    except Exception as e:
        print(f"\n[!] Не удалось запустить ngrok туннель: {e}\n")

    # Запуск сервера
    uvicorn.run("main:app", host="0.0.0.0", port=port)
