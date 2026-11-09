"""DM-only transport. Shared profile is NOT user isolation.

Admission is at-most-once within a bounded 7-day/10000-record replay window:
claim is committed BEFORE dispatch, so a crash can lose a claimed message.
No exactly-once promise; evicted/expired replays can be delivered again.
"""
import asyncio
from contextlib import suppress
from dataclasses import replace
import hashlib
from pathlib import Path
import secrets
import sqlite3
import time
import weakref
from httpx import AsyncClient as MediaClient
from agent.secret_scope import get_secret
from hermes_constants import get_hermes_home
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Conflict, InvalidToken, NetworkError
from telegram.ext import Application, TypeHandler

MAX_MEDIA = 5 * 1024 * 1024


class MultiBotAdapter(BasePlatformAdapter):
    # Upstream scoped locks self-reacquire for the same PID. Guard our own
    # adapters separately, and compare bundled's declared secret before polling.
    _owners = weakref.WeakValueDictionary()

    def __init__(self, config, *, slot, alias, allowed_users):
        config = replace(config, extra={**config.extra, 'allow_from': sorted(allowed_users)})
        super().__init__(config, Platform(f'telegram_{alias}'))
        self.alias = alias
        self.allowed_users = frozenset(allowed_users)
        self._token = get_secret(f'MULTIBOT_SLOT_{slot}_TOKEN')
        if not self._token:
            raise ValueError(f'missing declared token for slot {slot} ({alias})')
        self._identity = hashlib.sha256(self._token.encode()).hexdigest()
        self.application = self._build_application()
        self._buttons = {}
        self._lock_owned = False
        self._initialized = False
        self._fatal_task = None
        self._lifecycle = asyncio.Lock()
        self._media_lock = asyncio.Lock()

    def _build_application(self):
        app = Application.builder().token(self._token).build()
        app.add_handler(TypeHandler(Update, self.on_update))
        return app

    async def connect(self, *, is_reconnect=False):
        async with self._lifecycle:
            if self._lock_owned and self.application.running:
                return True
            self._fatal_error_code = self._fatal_error_message = None
            owner = self._owners.get(self._identity)
            if self._token == get_secret('TELEGRAM_BOT_TOKEN') or (owner is not None and owner is not self):
                self._set_fatal_error('duplicate_token', 'Telegram token is already configured for another bot', retryable=False)
                return False
            # No --replace/takeover authority, including when the runner arms it.
            self._platform_lock_takeover_allowed = False
            if not self._acquire_platform_lock('telegram-bot-token', self._token, 'Telegram bot token'):
                # Base records the identity even on failure: NEVER release it.
                self._platform_lock_identity = None
                return False
            self._lock_owned = True
            self._owners[self._identity] = self
            try:
                # PTB supports transport backoff in getUpdates; initialization gets
                # a short bounded retry, then the gateway can schedule reconnect.
                for attempt in range(3):
                    try:
                        await self.application.initialize()
                        self._initialized = True
                        await self.application.start()
                        await self.application.updater.start_polling(
                            allowed_updates=['message', 'callback_query'],
                            bootstrap_retries=0, error_callback=self._polling_error)
                        self._mark_connected()
                        return True
                    except NetworkError:
                        await self._cleanup()
                        if attempt == 2:
                            raise
                        self.application = self._build_application()
                        await asyncio.sleep(0.1 * (2 ** attempt))
            except (InvalidToken, Conflict) as exc:
                await self._cleanup()
                self._release_owned_lock()
                self._set_fatal_error(type(exc).__name__, 'Telegram rejected this bot; check token and competing pollers', retryable=False)
                return False
            except asyncio.CancelledError:
                await self._cleanup()
                self._release_owned_lock()
                raise
            except Exception:
                await self._cleanup()
                self._release_owned_lock()
                self._set_fatal_error('transport_start', 'Telegram transport could not start', retryable=True)
                return False

    def _polling_error(self, error):
        # PTB's network retry loop handles transient NetworkError. Do not log
        # SDK exceptions: request URLs may carry tokens.
        if isinstance(error, (InvalidToken, Conflict)) and not self._fatal_task:
            self._fatal_task = asyncio.create_task(self._fatal_polling(error))

    async def _fatal_polling(self, error):
        self._set_fatal_error(type(error).__name__, 'Telegram polling rejected; check token and competing pollers', retryable=False)
        await self._notify_fatal_error()
        await self.disconnect()

    async def _cleanup(self):
        # Each stage is attempted even after a previous stage failed.
        if self.application.updater.running:
            with suppress(Exception):
                await self.application.updater.stop()
        if self.application.running:
            with suppress(Exception):
                await self.application.stop()
        # Keep ownership and the original client if stop failed. Releasing the
        # token lock or rebuilding here could allow a second live poller.
        if self.application.updater.running or self.application.running:
            raise RuntimeError('Telegram transport did not stop; token lock retained')
        # initialize() can partially initialize HTTP clients before raising.
        with suppress(Exception):
            await self.application.shutdown()
        # Application.shutdown is a no-op if getMe failed during initialize;
        # Bot.shutdown still closes both initialized request pools in that case.
        with suppress(Exception):
            await self.application.bot.shutdown()
        self._initialized = False

    def _release_owned_lock(self):
        if self._lock_owned:
            self._release_platform_lock()
            self._lock_owned = False
        if self._owners.get(self._identity) is self:
            self._owners.pop(self._identity, None)

    async def disconnect(self):
        async with self._lifecycle:
            await self._cleanup()
            self._release_owned_lock()
            self._buttons.clear()
            self._mark_disconnected()
            # A stopped PTB Application is rebuilt for a clean reconnect.
            self.application = self._build_application()
            self._fatal_task = None

    def _allowed(self, user, chat):
        return bool(user and not user.is_bot and chat and chat.type == 'private'
                    and str(user.id) in self.allowed_users and chat.id == user.id)

    def _claim(self, update, msg):
        root = Path(get_hermes_home()) / 'multibot-data' / self._identity
        root.mkdir(parents=True, exist_ok=True)
        # Message timestamp prevents false suppression after Telegram resets IDs.
        key = f'{update.update_id}:{msg.chat.id}:{msg.message_id}:{msg.date.timestamp()}'
        now = time.time()
        with sqlite3.connect(root / 'replay.sqlite3') as db:
            db.execute('CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, claimed REAL NOT NULL)')
            db.execute('DELETE FROM seen WHERE claimed < ?', (now - 7 * 86400,))
            inserted = db.execute('INSERT OR IGNORE INTO seen VALUES (?, ?)', (key, now)).rowcount
            db.execute('DELETE FROM seen WHERE rowid NOT IN (SELECT rowid FROM seen ORDER BY rowid DESC LIMIT 10000)')
        return bool(inserted)

    async def _download(self, media):
        # Metadata is an early rejection only: the streaming byte cap is authoritative.
        if media.file_size is None or not 0 <= media.file_size <= MAX_MEDIA:
            return None
        async with self._media_lock:
            root = Path(get_hermes_home()) / 'multibot-media' / self.alias
            root.mkdir(parents=True, exist_ok=True)
            for path in root.iterdir():
                if path.is_file() and path.stat().st_mtime < time.time() - 86400:
                    path.unlink(missing_ok=True)
            files = [p for p in root.iterdir() if p.is_file()]
            if len(files) >= 32 or sum(p.stat().st_size for p in files) + MAX_MEDIA > 20 * 1024 * 1024:
                return None
            remote = await media.get_file()
            dest = root / (secrets.token_hex(16) + '.bin')
            try:
                # SDK returns the authenticated file URL. Never use the supplier
                # filename/MIME and never buffer an unbounded HTTP response.
                async with MediaClient(timeout=30, trust_env=False, follow_redirects=False) as client:
                    async with client.stream('GET', remote.file_path) as response:
                        response.raise_for_status()
                        size = 0
                        with dest.open('xb') as stream:
                            async for chunk in response.aiter_bytes(65536):
                                size += len(chunk)
                                if size > MAX_MEDIA:
                                    raise ValueError('media exceeds byte limit')
                                stream.write(chunk)
                return str(dest)
            except Exception:
                dest.unlink(missing_ok=True)
                return None
            except asyncio.CancelledError:
                dest.unlink(missing_ok=True)
                raise

    async def on_update(self, update, context):
        if update.callback_query:
            query = update.callback_query
            msg = query.message
            if not msg or not self._allowed(query.from_user, msg.chat):
                return
            binding = self._buttons.get(query.data)
            if binding != (str(msg.chat.id), str(query.from_user.id), str(msg.message_id)):
                return
            source = self.build_source(str(msg.chat.id), user_id=str(query.from_user.id))
            if self._authorization_check and not self._authorization_check(source.user_id, source.chat_type, source.chat_id):
                return
            self._buttons.pop(query.data, None)
            await query.answer('Demo acknowledged; no action executed.')
            return
        msg = update.message
        if not msg or not self._allowed(msg.from_user, msg.chat):
            return
        source = self.build_source(str(msg.chat.id), user_id=str(msg.from_user.id),
                                   user_name=msg.from_user.first_name, message_id=str(msg.message_id))
        if self._authorization_check and not self._authorization_check(source.user_id, source.chat_type, source.chat_id):
            return
        # No albums, voice/video, groups, edits or execution callbacks in this MVP.
        if msg.media_group_id:
            return
        media = msg.photo[-1] if msg.photo else msg.document
        if not media and not msg.text:
            return
        if not self._claim(update, msg):
            return
        paths, types = [], []
        kind = MessageType.TEXT
        if media:
            path = await self._download(media)
            if not path:
                return
            # Telegram's photo container is an image (JPEG), independent of
            # document-supplied MIME. Preserve only an allowlisted document
            # suffix, never its filename/path. Native gateway routes by MIME
            # first and needs text suffixes to expose documents correctly.
            suffix = '.jpg' if msg.photo else Path(msg.document.file_name or '').suffix.lower()
            safe_suffixes = {'.txt', '.md', '.csv', '.log', '.json', '.xml',
                             '.yaml', '.yml', '.toml', '.ini', '.cfg', '.pdf',
                             '.docx', '.xlsx'}
            if msg.photo or suffix in safe_suffixes:
                target = Path(path).with_suffix(suffix)
                Path(path).rename(target)
                path = str(target)
            paths.append(path)
            types.append('image/jpeg' if msg.photo else 'application/octet-stream')
            kind = MessageType.PHOTO if msg.photo else MessageType.DOCUMENT
        await self.handle_message(MessageEvent(
            text=msg.text or msg.caption or '', message_type=kind, source=source,
            user_id=str(msg.from_user.id), message_id=str(msg.message_id),
            media_urls=paths, media_types=types, raw_message=msg,
        ))

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        msg = None
        for offset in range(0, len(content), 4096):
            msg = await self.application.bot.send_message(chat_id=chat_id, text=content[offset:offset + 4096],
                                                         reply_to_message_id=int(reply_to) if reply_to is not None else None)
        return SendResult(success=msg is not None, message_id=str(msg.message_id) if msg else None)

    async def send_image(self, chat_id, image_url, caption=None, reply_to=None, metadata=None):
        msg = await self.application.bot.send_photo(chat_id=chat_id, photo=image_url,
            caption=caption, reply_to_message_id=int(reply_to) if reply_to is not None else None)
        return SendResult(success=True, message_id=str(msg.message_id))

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kwargs):
        with open(image_path, 'rb') as stream:
            msg = await self.application.bot.send_photo(chat_id=chat_id, photo=stream,
                caption=caption, reply_to_message_id=int(reply_to) if reply_to is not None else None)
        return SendResult(success=True, message_id=str(msg.message_id))

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None, **kwargs):
        with open(file_path, 'rb') as stream:
            msg = await self.application.bot.send_document(chat_id=chat_id, document=stream,
                caption=caption, filename=file_name, reply_to_message_id=int(reply_to) if reply_to is not None else None)
        return SendResult(success=True, message_id=str(msg.message_id))

    async def send_demo(self, chat_id, user_id):
        if str(user_id) not in self.allowed_users or str(chat_id) != str(user_id):
            raise ValueError('demo requires allowed private user')
        key = 'demo:' + secrets.token_hex(16)
        msg = await self.application.bot.send_message(chat_id=chat_id, text='Safe demo only',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Acknowledge demo', callback_data=key)]]))
        if len(self._buttons) >= 128:
            self._buttons.pop(next(iter(self._buttons)))
        self._buttons[key] = (str(chat_id), str(user_id), str(msg.message_id))
        return msg

    async def get_chat_info(self, chat_id):
        return {'name': str(chat_id), 'type': 'dm'}
