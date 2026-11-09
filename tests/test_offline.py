import json
from pathlib import Path
from urllib.parse import urlsplit
import pytest
from telegram.request import BaseRequest
from telegram import Update
from conftest import HOME

class OfflineRequest(BaseRequest):
    calls = []
    serial = 100
    @property
    def read_timeout(self): return 5
    async def initialize(self): pass
    async def shutdown(self): pass
    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit('/', 1)[-1]
        bot = urlsplit(url).path.split('/')[1]
        params = request_data.parameters if request_data else {}
        self.calls.append((bot, endpoint, params))
        if '/file/' in url:
            from io import BytesIO
            from PIL import Image
            image = BytesIO()
            Image.new('RGB', (1, 1), 'white').save(image, format='JPEG')
            return 200, image.getvalue()
        if endpoint == 'getMe':
            result = {'id': int(bot.split(':')[0][3:]), 'is_bot': True, 'first_name': 'Offline', 'username': 'offline_bot'}
        elif endpoint == 'getUpdates': result = []
        elif endpoint in {'deleteWebhook','answerCallbackQuery','sendChatAction'}: result = True
        elif endpoint == 'getFile': result = {'file_id': 'f', 'file_unique_id': 'u', 'file_size': 13, 'file_path': 'offline.bin'}
        elif endpoint in {'sendMessage','sendPhoto','sendDocument'}:
            type(self).serial += 1
            result = {'message_id': self.serial, 'date': 1, 'chat': {'id': int(params['chat_id']), 'type': 'private'}, 'text': params.get('text', '')}
        else: raise AssertionError(f'unexpected Bot API {endpoint}')
        return 200, json.dumps({'ok': True, 'result': result}).encode()


def test_cold_discovery():
    from hermes_cli.plugins import get_plugin_manager
    from gateway.config import load_gateway_config, Platform
    from gateway.platform_registry import platform_registry
    manager = get_plugin_manager()
    manager.discover_and_load()
    entry = manager._plugins.get('hermes-multibot')
    assert entry and entry.enabled, {k: v.error for k,v in manager._plugins.items() if v.error}
    assert not entry.deferred
    config = load_gateway_config()
    for alias in ['alpha','beta','gamma']:
        name = f'telegram_{alias}'
        assert platform_registry.get(name) is not None
        assert config.platforms[Platform(name)].enabled


@pytest.mark.asyncio
async def test_gateway_transport(monkeypatch):
    test_cold_discovery()
    # Replace only PTB HTTP transport construction. Real Application, Bot and handlers remain.
    from telegram.ext import ApplicationBuilder
    monkeypatch.setattr('telegram.ext._applicationbuilder.HTTPXRequest', lambda *a, **kw: OfflineRequest())
    import httpx
    original_client = httpx.AsyncClient
    from hermes_cli.plugins import get_plugin_manager
    media_module = __import__(get_plugin_manager()._plugins['hermes-multibot'].module.MultiBotAdapter.__module__, fromlist=['MediaClient'])
    from io import BytesIO
    from PIL import Image
    fixture_image = BytesIO()
    Image.new('RGB', (1, 1), 'white').save(fixture_image, format='JPEG')
    monkeypatch.setattr(media_module, 'MediaClient', lambda *a, **kw: original_client(*a, **{**kw, 'transport': httpx.MockTransport(lambda request: httpx.Response(200, content=fixture_image.getvalue()))}))
    from gateway.run import GatewayRunner
    from gateway.config import load_gateway_config, Platform
    runner = GatewayRunner(load_gateway_config())
    adapters = {}
    for alias in ['alpha','beta','gamma']:
        p = Platform(f'telegram_{alias}')
        adapter = runner._create_adapter(p, runner.config.platforms[p])
        assert adapter is not None
        runner.adapters[p] = adapter
        runner._wire_adapter_handlers(adapter)
        await adapter.application.initialize()
        adapters[alias] = adapter
        if alias == 'alpha':
            source = adapter.build_source('42', user_id='42')
            assert runner._is_user_authorized(source), (adapter.config.extra, source)
            assert adapter._authorization_check('42', 'dm', '42')

    # LLM completion boundary only; no gateway/session/auth mocks.
    from openai.types.chat import ChatCompletion
    llm_inputs = []
    def completion(*args, **kwargs):
        llm_inputs.append(json.dumps(kwargs, default=str))
        return ChatCompletion(id='offline', created=1, model='gpt-4.1-mini', object='chat.completion',
            choices=[{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'OFFLINE_REPLY'}}],
            usage={'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13})
    monkeypatch.setattr('openai.resources.chat.completions.Completions.create', completion)
    from types import SimpleNamespace as NS
    def responses(*args, **kwargs):
        llm_inputs.append(json.dumps(kwargs, default=str))
        response = NS(id='offline-response', model='gpt-4.1-mini', status='completed',
            output=[NS(type='message', id='msg', role='assistant', status='completed',
                       content=[NS(type='output_text', text='OFFLINE_REPLY', annotations=[])])],
            usage=NS(input_tokens=10, output_tokens=3, total_tokens=13))
        return iter([NS(type='response.output_item.done', item=response.output[0], output_index=0), NS(type='response.completed', response=response)])
    monkeypatch.setattr('openai.resources.responses.Responses.create', responses)

    async def deliver(alias, user=42, chat_type='private', edited=False, bot=False, media=None):
        msg = {'message_id': OfflineRequest.serial + 1000, 'date': 1,
               'from': {'id': user, 'first_name': 'Test', 'is_bot': bot},
               'chat': {'id': user, 'type': chat_type}, 'text': 'Reply offline please'}
        if media:
            msg.pop('text')
            if media == 'photo': msg['photo'] = [{'file_id':'f','file_unique_id':'u','width':1,'height':1,'file_size':13}]
            else: msg['document'] = {'file_id':'f','file_unique_id':'u','file_size':13,'mime_type':'application/octet-stream','file_name':'../bad.bin'}
        data = {'update_id': OfflineRequest.serial, 'edited_message' if edited else 'message': msg}
        update = Update.de_json(data, adapters[alias].application.bot)
        await adapters[alias].application.process_update(update)
        import asyncio
        for _ in range(600):
            if not adapters[alias]._background_tasks:
                break
            await asyncio.sleep(0.05)

    try:
        await deliver('alpha')
        await deliver('beta')
        sends = [(b,p) for b,e,p in OfflineRequest.calls if e == 'sendMessage' and 'OFFLINE_REPLY' in p.get('text','')]
        assert len(sends) == 2, OfflineRequest.calls
        assert [b for b, _ in sends] == [
            'bot10001:FAKE_ALPHA_TOKEN_OFFLINE_ONLY',
            'bot10002:FAKE_BETA_TOKEN_OFFLINE_ONLY',
        ], sends
        assert all(str(p['chat_id']) == '42' for _, p in sends)
        assert adapters['alpha'].gateway_runner is runner
        assert adapters['beta'].gateway_runner is runner
        print('BOT_REPLY_ROUTES', json.dumps([{'bot_id': b.split(':')[0], 'chat_id': p['chat_id']} for b,p in sends]))
        for platform_label in ('Telegram_Alpha', 'Telegram_Beta'):
            assert any(platform_label in payload
                       and 'OFFLINE_SHARED_MEMORY_SENTINEL' in payload
                       and 'OFFLINE_SHARED_SKILL_SENTINEL' in payload
                       for payload in llm_inputs), (platform_label, llm_inputs)
        print('SHARED_PROFILE_PROMPTS', 'alpha/beta: same scratch memory + skill catalog')
        # Read actual persisted session mapping, not calculated sample keys.
        files = list(HOME.rglob('sessions.json'))
        assert files, list(HOME.rglob('*'))
        persisted = json.loads(files[0].read_text())
        print('PERSISTED_SESSIONS', json.dumps(persisted))
        serialized = json.dumps(persisted)
        assert 'telegram_alpha' in serialized and 'telegram_beta' in serialized
        alpha = persisted['agent:main:telegram_alpha:dm:42']
        beta = persisted['agent:main:telegram_beta:dm:42']
        assert alpha['session_id'] != beta['session_id']
        import sqlite3
        with sqlite3.connect(HOME / 'state.db') as db:
            rows = db.execute('select id from sessions where id in (?,?)',
                              (alpha['session_id'], beta['session_id'])).fetchall()
            assert len(rows) == 2
        # Real gateway independently rejects an out-of-list source (not trusted).
        assert not runner._is_user_authorized(adapters['alpha'].build_source('44', user_id='44'))
        assert not runner._is_user_authorized(adapters['beta'].build_source('43', user_id='43'))
        before = len(OfflineRequest.calls)
        for alias,user in [('alpha',44),('beta',43),('alpha',999),('gamma',42)]:
            await deliver(alias,user,media='document')
        await deliver('alpha',chat_type='group',media='photo')
        await deliver('alpha',edited=True,media='document')
        await deliver('alpha',bot=True,media='photo')
        unknown = Update.de_json({'update_id': 99999, 'message': {
            'message_id': 99999, 'date': 1, 'chat': {'id': 42, 'type': 'private'},
            'document': {'file_id':'f', 'file_unique_id':'u', 'file_size':13}}},
            adapters['alpha'].application.bot)
        await adapters['alpha'].application.process_update(unknown)
        assert len(OfflineRequest.calls) == before
        await deliver('gamma',45)
        assert any(b.startswith('bot10003:') and e=='sendMessage' for b,e,p in OfflineRequest.calls)
        await deliver('alpha',media='document')
        assert list((HOME/'multibot-media'/'alpha').glob('*.bin'))
        # Photo admission/download and native outbound uploads; no vision model claim.
        await deliver('alpha', media='photo')
        photo = next((HOME/'multibot-media'/'alpha').glob('*.jpg'))
        assert (await adapters['alpha'].send_image_file('42', photo)).success
        document = next((HOME/'multibot-media'/'alpha').glob('*.bin'))
        assert (await adapters['alpha'].send_document('42', document)).success
        assert await adapters['beta'].application.bot.get_updates() == ()
        demo = await adapters['alpha'].send_demo('42','42')
        key = next(iter(adapters['alpha']._buttons))
        async def callback(alias='alpha', user=42, mid=None):
            data={'update_id':999,'callback_query':{'id':'cb','chat_instance':'i','data':key,
                'from':{'id':user,'first_name':'Test','is_bot':False},
                'message':{'message_id':mid or demo.message_id,'date':1,'chat':{'id':42,'type':'private'}}}}
            await adapters[alias].application.process_update(Update.de_json(data,adapters[alias].application.bot))
        count = sum(e=='answerCallbackQuery' for b,e,p in OfflineRequest.calls)
        await callback(user=44)
        await callback(mid=999)
        await callback(alias='beta')
        assert sum(e=='answerCallbackQuery' for b,e,p in OfflineRequest.calls)==count
        await callback()
        assert sum(e=='answerCallbackQuery' for b,e,p in OfflineRequest.calls)==count+1
        await callback()
        assert sum(e=='answerCallbackQuery' for b,e,p in OfflineRequest.calls)==count+1
    finally:
        for adapter in adapters.values():
            await adapter.application.shutdown()
