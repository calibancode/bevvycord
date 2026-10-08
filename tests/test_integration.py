"""Offline adapter checks; these are not live Discord validation."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import httpx
import pytest

from bevvycord.provider import Provider


def test_provider_keeps_only_completed_final_content_and_requests_no_stream(monkeypatch):
    monkeypatch.setenv('BEVVYCORD_TEST_KEY', 'test-only-key')
    async def scenario():
        provider = Provider({'base_url': 'https://example.invalid/v1', 'api_key_env': 'BEVVYCORD_TEST_KEY', 'model': 'test'})
        await provider.client.aclose()
        def handle(request):
            import json
            body = json.loads(request.content)
            assert request.url.path == '/v1/chat/completions'
            assert body['stream'] is False
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                'content': 'final answer', 'reasoning_content': 'private reasoning'}}], 'usage': {}})
        provider.client = httpx.AsyncClient(base_url='https://example.invalid/v1/', transport=httpx.MockTransport(handle))
        try:
            assert await provider.generate([{'role': 'user', 'content': 'hi'}]) == 'final answer'
        finally:
            await provider.close()
    asyncio.run(scenario())


def test_incomplete_provider_response_is_not_accepted(monkeypatch):
    monkeypatch.setenv('BEVVYCORD_TEST_KEY', 'test-only-key')
    async def scenario():
        provider = Provider({'base_url': 'https://example.invalid', 'api_key_env': 'BEVVYCORD_TEST_KEY', 'model': 'test'})
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(base_url='https://example.invalid/', transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': 'partial'}}]})))
        try:
            with pytest.raises(RuntimeError, match='did not complete'):
                await provider.generate([])
        finally:
            await provider.close()
    asyncio.run(scenario())


def test_discord_reply_flow_and_testing_channel_isolation(tmp_path):
    discord = pytest.importorskip('discord')
    from bevvycord.bot import CharacterBot
    from bevvycord.storage import Store

    class Typing:
        async def __aenter__(self): pass
        async def __aexit__(self, *args): pass

    class Channel:
        def __init__(self, channel_id):
            self.id = channel_id
            self.messages = {}
        def typing(self): return Typing()
        async def history(self, before, limit):
            for item in sorted(self.messages.values(), key=lambda m: m.id, reverse=True):
                if item.id < before.id:
                    yield item
        async def fetch_message(self, message_id):
            return self.messages[message_id]

    class DiscordMessage:
        def __init__(self, message_id, channel, text, bot=False, reference=None):
            self.id, self.channel, self.content = message_id, channel, text
            self.author = NS(id=9 if bot else 1, display_name='Rowan' if bot else 'Bevvy', bot=bot)
            self.guild, self.webhook_id = NS(id=1), None
            self.created_at = datetime.now(timezone.utc)
            self.reference = reference
            self.type = discord.MessageType.reply if reference else discord.MessageType.default
            self.mentions = [] if bot else [NS(id=9)]
            self.components, self.embeds, self.attachments, self.stickers = [], [], [], []
            channel.messages[message_id] = self
        async def reply(self, *, content, mention_author, allowed_mentions=None):
            assert mention_author is False
            assert allowed_mentions is not None
            assert allowed_mentions.everyone is False
            assert allowed_mentions.users is False
            return DiscordMessage(max(self.channel.messages) + 1, self.channel, content, bot=True)

    class FakeProvider:
        def __init__(self): self.requests = []
        async def generate(self, messages):
            self.requests.append(messages)
            return 'A completed character reply.'

    async def scenario():
        store, provider = Store(tmp_path, 'rowan'), FakeProvider()
        config = {'character': {'prompt': 'You are Rowan'}, 'allowed_channel_ids': {100, 200},
                  'allowed_user_ids': set(), 'context': {'soft_chunks': 20, 'hard_chunks': 40,
                  'max_fetch_messages': 1000, 'max_prompt_chars': 200000}, 'memory': {'enabled': False}}
        bot = CharacterBot(config, store, provider)
        bot._connection.user = NS(id=9)
        main, testing = Channel(100), Channel(200)
        try:
            await bot.on_message(DiscordMessage(1, main, 'MAIN CHANNEL PRIVATE TOPIC'))
            await bot.on_message(DiscordMessage(10, testing, 'TEST CHANNEL TOPIC'))
            assert len(provider.requests) == 2
            assert 'Your Discord speaker ID is bot:9' in provider.requests[1][0]['content']
            assert 'MAIN CHANNEL PRIVATE TOPIC' not in provider.requests[1][1]['content']
            assert store.previous(100).last_response_id == 2
            assert store.previous(200).last_response_id == 11
            # Reading another bot is allowed; receiving its ping never generates.
            bot_message = DiscordMessage(12, testing, '<@9> respond please', bot=True)
            bot_message.mentions = [NS(id=9)]
            await bot.on_message(bot_message)
            assert len(provider.requests) == 2
            # Uncached reference with reply notifications disabled still invokes.
            followup = DiscordMessage(13, testing, 'Continue', reference=NS(message_id=11, resolved=None))
            followup.mentions = []
            await bot.on_message(followup)
            assert len(provider.requests) == 3
            assert '<@9> respond please' in '\n'.join(m['content'] for m in provider.requests[2])
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_structured_provider_passes_tools_and_original_reasoning(monkeypatch):
    import json
    monkeypatch.setenv('BEVVYCORD_TEST_KEY', 'test-only-key')
    async def scenario():
        provider = Provider({'base_url': 'https://example.invalid', 'api_key_env': 'BEVVYCORD_TEST_KEY',
                             'model': 'deepseek-flash', 'parameters': {'thinking': {'type': 'enabled'}}})
        await provider.client.aclose()
        calls = [{'id': 'call-1', 'type': 'function', 'function': {'name': 'example', 'arguments': '{}'}}]
        original = {'role': 'assistant', 'content': None, 'reasoning_content': 'original private field', 'tool_calls': calls}
        seen = []
        def transport(request):
            body = json.loads(request.content)
            seen.append(body)
            assert body['model'] == 'deepseek-flash'
            assert body['thinking'] == {'type': 'enabled'}
            assert body['tools'][0]['function']['name'] == 'example'
            return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': original}]})
        provider.client = httpx.AsyncClient(base_url='https://example.invalid/', transport=httpx.MockTransport(transport))
        try:
            definitions = [{'type': 'function', 'function': {'name': 'example', 'parameters': {'type': 'object'}}}]
            result = await provider.complete([{'role': 'user', 'content': 'hi'}], tools=definitions)
            assert result['message'] == original
            await provider.complete([{'role': 'user', 'content': 'hi'}, result['message'],
                                     {'role': 'tool', 'tool_call_id': 'call-1', 'content': '{}'}], tools=definitions)
            assert seen[-1]['messages'][1]['reasoning_content'] == original['reasoning_content']
            await provider.complete([{'role': 'user', 'content': 'finish'}], tools=definitions, tool_choice='none')
            assert seen[-1]['tool_choice'] == 'none'
            assert seen[-1]['thinking'] == {'type': 'enabled'}
        finally:
            await provider.close()
    asyncio.run(scenario())
