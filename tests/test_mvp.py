"""Bounded MVP regressions; all credentials and HTTP responses are offline fixtures."""
import asyncio
import importlib
import sqlite3
from unittest.mock import AsyncMock
import httpx
import pytest
from telegram import Update
from telegram.error import Conflict, InvalidToken, NetworkError
from conftest import HOME, ROOT
from test_offline import OfflineRequest, test_cold_discovery as discover


def test_public_mvp_contract():
    from ruamel.yaml import YAML
    manifest = YAML(typ='safe').load((ROOT / 'plugin/plugin.yaml').read_text())
    assert manifest['name'] == 'hermes-multibot'
    assert manifest['version'] == '0.2.0'
    assert [x['name'] for x in manifest['optional_env']] == [f'MULTIBOT_SLOT_{i}_TOKEN' for i in range(1, 9)]
    from gateway.platforms.base import BasePlatformAdapter
    cls = adapter_class()
    assert cls.send_image is not BasePlatformAdapter.send_image


def adapter_class():
    discover()
    from hermes_cli.plugins import get_plugin_manager
    return get_plugin_manager()._plugins['hermes-multibot'].module.MultiBotAdapter


@pytest.fixture
def factory(monkeypatch):
    cls = adapter_class()
    monkeypatch.setattr('telegram.ext._applicationbuilder.HTTPXRequest', lambda *a, **kw: OfflineRequest())
    from gateway.config import load_gateway_config, Platform
    config = load_gateway_config()
    def make(alias='alpha', slot=1):
        return cls(config.platforms[Platform('telegram_alpha')], slot=slot, alias=alias, allowed_users=frozenset({'42'}))
    return make


@pytest.mark.asyncio
async def test_connect_stop_reconnect_and_duplicate_tokens(factory, monkeypatch):
    a, duplicate, other = factory(), factory('beta'), factory('beta', 2)
    # No tight idle poll loop in this test: getUpdates remains real SDK HTTP.
    original = OfflineRequest.do_request
    async def paced(self, url, method, **kw):
        if url.endswith('/getUpdates'):
            await asyncio.sleep(0.01)
        return await original(self, url, method, **kw)
    monkeypatch.setattr(OfflineRequest, 'do_request', paced)
    try:
        assert await a.connect()
        assert not await duplicate.connect()
        assert duplicate.has_fatal_error
        await duplicate.disconnect()  # must NOT release a's lock or ownership
        assert a._lock_owned
        assert not await duplicate.connect()
        assert await other.connect()
        await a.disconnect()
        assert await a.connect(is_reconnect=True)
        assert other.application.running
    finally:
        await a.disconnect()
        await duplicate.disconnect()
        await other.disconnect()


@pytest.mark.asyncio
async def test_bundled_duplicate_fails_before_http(factory, monkeypatch):
    a = factory()
    module = importlib.import_module(type(a).__module__)
    original = module.get_secret
    monkeypatch.setattr(module, 'get_secret', lambda name: a._token if name == 'TELEGRAM_BOT_TOKEN' else original(name))
    before = len(OfflineRequest.calls)
    assert not await a.connect()
    assert len(OfflineRequest.calls) == before
    assert not a._lock_owned
    await a.disconnect()


@pytest.mark.asyncio
async def test_invalid_token_cleanup_and_other_bot_survives(factory, monkeypatch):
    a, other = factory(), factory('beta', 2)
    original = OfflineRequest.do_request
    closed = []
    async def shutdown(self):
        closed.append(self)
    monkeypatch.setattr(OfflineRequest, 'shutdown', shutdown)
    async def invalid(self, url, method, **kw):
        if url.endswith('/getMe') and '10001:' in url:
            return 401, b'{"ok":false,"error_code":401,"description":"Unauthorized"}'
        if url.endswith('/getUpdates'):
            await asyncio.sleep(0.01)
        return await original(self, url, method, **kw)
    monkeypatch.setattr(OfflineRequest, 'do_request', invalid)
    try:
        assert await other.connect()
        assert not await a.connect()
        assert a.has_fatal_error and not a.fatal_error_retryable
        assert not a._lock_owned
        assert len(closed) >= 2
        assert other.application.running
    finally:
        await a.disconnect()
        await other.disconnect()


@pytest.mark.asyncio
async def test_transient_startup_retry_then_polling_fatal_callback(factory, monkeypatch):
    a = factory()
    original = OfflineRequest.do_request
    tries = 0
    async def flaky(self, url, method, **kw):
        nonlocal tries
        if url.endswith('/getMe'):
            tries += 1
            if tries == 1:
                raise NetworkError('offline transient')
        if url.endswith('/getUpdates'):
            await asyncio.sleep(0.01)
        return await original(self, url, method, **kw)
    monkeypatch.setattr(OfflineRequest, 'do_request', flaky)
    notified = []
    a.set_fatal_error_handler(lambda adapter: notified.append(adapter.platform.value))
    try:
        assert await a.connect()
        assert tries >= 2
        a._polling_error(NetworkError('temporary'))
        await asyncio.sleep(0)
        assert a.application.running and not notified
        a._polling_error(Conflict('competing poller'))
        task = a._fatal_task
        await task
        assert notified == ['telegram_alpha']
        assert not a._lock_owned and not a.application.running
    finally:
        await a.disconnect()


@pytest.mark.asyncio
async def test_real_cross_process_lock_collision_never_releases_holder(factory):
    import os
    import subprocess
    import sys
    a = factory()
    code = '''from gateway.status import acquire_scoped_lock
from gateway.status_scoped_locks import release_scoped_lock
import sys
identity = sys.stdin.readline().strip()
ok, _ = acquire_scoped_lock('telegram-bot-token', identity, metadata={'owner_kind':'process'})
print('locked' if ok else 'failed', flush=True)
sys.stdin.readline()
release_scoped_lock('telegram-bot-token', identity)
'''
    child = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
    try:
        child.stdin.write(a._token + '\n')
        child.stdin.flush()
        assert child.stdout.readline().strip() == 'locked'
        a._platform_lock_takeover_allowed = True  # even a runner --replace must NOT kill holder
        assert not await a.connect()
        await a.disconnect()
        assert child.poll() is None
        # An actual second lock attempt must still see the original holder.
        from gateway.status import acquire_scoped_lock
        ok, holder = acquire_scoped_lock('telegram-bot-token', a._token)
        assert not ok and holder['pid'] == child.pid
    finally:
        child.communicate('\n', timeout=10)
        assert child.returncode == 0
        await a.disconnect()


def message(a, update_id=501, message_id=501, user=42, media=None, date=123):
    msg = {'message_id':message_id,'date':date,'chat':{'id':user,'type':'private'},
           'from':{'id':user,'first_name':'Offline','is_bot':False},'text':'test'}
    if media:
        msg.pop('text')
        msg['document'] = {'file_id':'f','file_unique_id':'u','file_size':1,'file_name':'../../evil.jpg','mime_type':'image/jpeg'}
    return Update.de_json({'update_id':update_id,'message':msg}, a.application.bot)


@pytest.mark.asyncio
async def test_persisted_replay_acl_before_getfile_and_id_reset(factory):
    a = factory()
    a.handle_message = AsyncMock()
    a.set_authorization_check(lambda *args: True)  # global grants cannot widen strict ACL
    before = len(OfflineRequest.calls)
    await a.on_update(message(a, user=999, media=True), None)
    assert len(OfflineRequest.calls) == before
    await a.on_update(message(a), None)
    assert a.handle_message.await_count == 1
    recreated = factory()
    recreated.handle_message = AsyncMock()
    await recreated.on_update(message(recreated), None)
    assert recreated.handle_message.await_count == 0
    await recreated.on_update(message(recreated, date=999999), None)
    assert recreated.handle_message.await_count == 1
    dbpath = HOME / 'multibot-data' / a._identity / 'replay.sqlite3'
    with sqlite3.connect(dbpath) as db:
        db.executemany('INSERT OR IGNORE INTO seen VALUES (?, ?)', ((f'old{i}',0) for i in range(10020)))
    await recreated.on_update(message(recreated, update_id=502), None)
    with sqlite3.connect(dbpath) as db:
        assert db.execute('SELECT count(*) FROM seen').fetchone()[0] <= 10000
    await a.disconnect()
    await recreated.disconnect()


@pytest.mark.asyncio
async def test_media_public_outbound_origin_and_text_split(factory, tmp_path):
    a, b = factory(), factory('beta', 2)
    path = tmp_path / 'test.bin'
    path.write_bytes(b'fixture')
    for adapter in (a,b):
        await adapter.application.initialize()
    try:
        before = len(OfflineRequest.calls)
        assert (await a.send_image('42','https://example.invalid/picture.png', caption='caption', reply_to='7')).success
        assert (await b.send_document('42',path, file_name='safe.bin', reply_to='8')).success
        assert (await b.send_image_file('42',path)).success
        assert (await a.send('42','x'*9000)).success
        calls = OfflineRequest.calls[before:]
        assert [(bot.split(':')[0], endpoint) for bot,endpoint,_ in calls[:3]] == [
            ('bot10001','sendPhoto'),('bot10002','sendDocument'),('bot10002','sendPhoto')]
        assert [len(params['text']) for _,endpoint,params in calls if endpoint == 'sendMessage'] == [4096,4096,808]
        assert calls[0][2]['reply_parameters']['message_id'] == 7
        assert calls[1][2]['reply_parameters']['message_id'] == 8
    finally:
        await a.disconnect()
        await b.disconnect()


@pytest.mark.asyncio
async def test_media_hard_byte_cap_quota_and_cleanup(factory, monkeypatch):
    a = factory()
    await a.application.initialize()
    module = importlib.import_module(type(a).__module__)
    client = httpx.AsyncClient
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(82):
                yield b'x'*65536
    monkeypatch.setattr(module, 'MediaClient', lambda **kw: client(**kw,transport=httpx.MockTransport(lambda req: httpx.Response(200,stream=Chunks()))))
    a.handle_message = AsyncMock()
    root = HOME/'multibot-media'/'alpha'
    before = set(root.iterdir()) if root.exists() else set()
    try:
        await a.on_update(message(a,update_id=800,message_id=800,media=True),None)
        assert not a.handle_message.called
        assert set(root.iterdir()) == before
        for i in range(32):
            (root / f'quota{i}.bin').write_bytes(b'quota')
        count = len(OfflineRequest.calls)
        await a.on_update(message(a,update_id=801,message_id=801,media=True),None)
        assert len(OfflineRequest.calls) == count  # reject before getFile
    finally:
        for path in root.glob('quota*.bin'):
            path.unlink()
        await a.disconnect()
