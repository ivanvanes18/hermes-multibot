"""Regression: native gateway must recognize accepted image/text attachments."""
from unittest.mock import AsyncMock
import httpx
import importlib
import pytest
from telegram import Update
from test_mvp import factory  # pytest fixture
from test_offline import OfflineRequest

@pytest.mark.asyncio
async def test_photo_is_native_image_document_keeps_safe_suffix(factory, monkeypatch):
    from gateway.run import _event_media_is_image, GatewayRunner
    a = factory()
    await a.application.initialize()
    module = importlib.import_module(type(a).__module__)
    client = httpx.AsyncClient
    from io import BytesIO
    from PIL import Image
    image = BytesIO()
    Image.new('RGB', (2, 2), 'white').save(image, format='JPEG')
    monkeypatch.setattr(module, 'MediaClient', lambda **kw: client(**kw, transport=httpx.MockTransport(lambda request: httpx.Response(200,content=image.getvalue()))))
    a.handle_message = AsyncMock()
    def update(mid, photo):
        msg = {'message_id':mid,'date':1000,'from':{'id':42,'is_bot':False,'first_name':'Test'},'chat':{'id':42,'type':'private'},'caption':'describe attachment'}
        if photo:
            msg['photo']=[{'file_id':'f','file_unique_id':'u','width':2,'height':2,'file_size':1000}]
        else:
            msg['document']={'file_id':'f','file_unique_id':'u','file_size':1000,'file_name':'../../test.txt','mime_type':'image/jpeg'}
        return Update.de_json({'update_id':mid,'message':msg},a.application.bot)
    try:
        await a.on_update(update(91001, True),None)
        event=a.handle_message.await_args.args[0]
        assert _event_media_is_image(event,0), 'gateway treats accepted photo as document'
        assert event.media_urls[0].endswith('.jpg')
        await a.on_update(update(91002,False),None)
        doc=a.handle_message.await_args.args[0]
        assert not _event_media_is_image(doc,0), 'supplier MIME must not masquerade as photo'
        assert doc.media_urls[0].endswith('.txt')
        assert '..' not in doc.media_urls[0]
    finally:
        await a.disconnect()
