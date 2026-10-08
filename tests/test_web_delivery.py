import asyncio
import json
from types import SimpleNamespace as NS

import httpx

from bevvycord.bot import CharacterBot
from bevvycord.plugins import fetch
from bevvycord.storage import Store
from test_agent_flow import Channel, Message, config
from test_runtime import call, response, FINAL


def test_image_search_fetch_and_discord_delivery(tmp_path, monkeypatch):
    monkeypatch.setenv('BEVVYCORD_SEARCH_URL', 'http://search.example.invalid')
    original_client = httpx.AsyncClient
    image = b'\x89PNG\r\n\x1a\nBUDDY'
    def search_server(request):
        assert request.url.params['categories'] == 'images'
        return httpx.Response(200, json={'results': [
            {'title': 'Buddy Thunderstruck', 'url': 'https://example.org/buddy',
             'img_src': 'https://example.org/buddy.png'}]})
    # Search uses its operator endpoint; fetch uses a separate guarded transport.
    def client(**kwargs):
        if 'transport' not in kwargs:
            kwargs['transport'] = httpx.MockTransport(search_server)
        return original_client(**kwargs)
    monkeypatch.setattr('bevvycord.plugins.search.httpx.AsyncClient', client)
    monkeypatch.setattr(fetch, 'PublicTransport', lambda: httpx.MockTransport(
        lambda req: httpx.Response(200, headers={'content-type': 'image/png'}, stream=httpx.ByteStream(image))))
    class Provider:
        def __init__(self): self.steps = 0
        async def complete(self, messages, **kwargs):
            self.steps += 1
            if self.steps == 1:
                return response(call('web_search', {'query': 'Buddy Thunderstruck', 'category': 'images'}))
            if self.steps == 2:
                url = json.loads(messages[-1]['content'])['results'][0]['image_url']
                return response(call('web_fetch', {'url': url}, 'download'))
            if self.steps == 3:
                path = json.loads(messages[-1]['content'])['path']
                return response(call('return_file', {'path': path, 'filename': 'buddy.png'}, 'attach'))
            assert kwargs['tool_choice'] == 'auto'
            return FINAL
    async def scenario():
        cfg = config()
        cfg['tools'].update(plugins=['bevvycord.plugins.search'], max_steps=4)
        store, channel = Store(tmp_path, 'rowan'), Channel()
        bot = CharacterBot(cfg, store, Provider())
        bot._connection.user = NS(id=9)
        try:
            await bot.on_message(Message(channel, '<@9> find and post an image of Buddy Thunderstruck'))
            assert channel.files == [('buddy.png', image)]
            outgoing = [m for m in channel.messages.values() if m.author.bot]
            assert len(outgoing) == 1
            assert outgoing[0].content == 'Done.'
            assert outgoing[0].attachments[0].filename == 'buddy.png'
            assert len(channel.sent_calls) == 1
            assert store.db.execute('SELECT state FROM jobs').fetchall() == [('complete',)]
            archive, _, _ = store.archive(100)
            assert 'buddy.png' in archive
            assert 'web_fetch' not in archive and 'PRIVATE INTERNAL REASONING' not in archive
            assert store.previous(100)
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())
