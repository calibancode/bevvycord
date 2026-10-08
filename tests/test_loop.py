import json
"""A full offline conversation lifecycle through the actual bot event handler."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

discord = pytest.importorskip('discord')
from bevvycord.bot import CharacterBot
from bevvycord.context import chunks
from bevvycord.memory import rebuild_memory
from bevvycord.storage import Store


def payload(messages):
    return '\n'.join(m['content'] for m in messages)


def test_full_conversation_loop_with_memory_restart_and_absence(tmp_path, monkeypatch):
    class World:
        next_id = 0
        def message(self, channel, text, author=1, invoke=False):
            self.next_id += 1
            return Message(self.next_id, channel, text, author, invoke)

    world = World()

    class Channel:
        def __init__(self, channel_id):
            self.id, self.messages, self.typing_active = channel_id, {}, False
        def typing(self):
            channel = self
            class Indicator:
                async def __aenter__(self): channel.typing_active = True
                async def __aexit__(self, *args): channel.typing_active = False
            return Indicator()
        async def history(self, before, limit):
            items = sorted((m for m in self.messages.values() if m.id < before.id), key=lambda m: m.id, reverse=True)
            for item in items[:limit]:
                yield item
        async def fetch_message(self, message_id): return self.messages[message_id]

    class Message:
        def __init__(self, message_id, channel, text, author, invoke):
            self.id, self.channel, self.content = message_id, channel, text
            self.author = NS(id=author, display_name=f'speaker-{author}', bot=author in (9, 10))
            self.guild, self.webhook_id, self.reference = NS(id=50), None, None
            self.type = discord.MessageType.default
            self.created_at = datetime.now(timezone.utc)
            self.mentions = [NS(id=9)] if invoke else []
            self.components, self.embeds, self.attachments, self.stickers = [], [], [], []
            channel.messages[message_id] = self
        async def reply(self, content, mention_author=False, allowed_mentions=None):
            assert allowed_mentions.users is False and allowed_mentions.everyone is False
            assert mention_author is False
            return world.message(self.channel, content, author=9)

    class Provider:
        def __init__(self):
            self.requests, self.inject, self.fail = [], None, False
        async def generate(self, messages, **kwargs):
            self.requests.append(messages)
            if '<participation_archive>' in messages[1]['content']:
                # A channel without MEMORY.md gets a full first write.
                assert '<current_memory>\n(empty)\n</current_memory>' in messages[1]['content']
                return '- I remember the testing channel.'
            assert main.typing_active or testing.typing_active
            if self.inject:
                world.message(main, 'MESSAGE ARRIVED WHILE THINKING', author=2)
                self.inject = None
            if self.fail:
                self.fail = False
                raise RuntimeError('Simulated provider failure')
            return 'My character reply.'
        async def complete(self, messages, tools=None, **kwargs):
            # Scheduled memory updates edit the existing file through tools.
            self.requests.append(messages)
            assert [t['function']['name'] for t in tools] == ['replace', 'append']
            assert '<current_memory>\n- OLD LINE\n</current_memory>' in messages[1]['content']
            assert '<new_conversation>' in messages[1]['content']
            if messages[-1]['role'] == 'tool':
                assert json.loads(messages[-1]['content']) == {'status': 'replaced'}
                return {'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'Updated.'}}
            edit = {'old': '- OLD LINE', 'new': '- I remember our ongoing conversation.'}
            return {'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'e1', 'type': 'function', 'function': {'name': 'replace', 'arguments': json.dumps(edit)}}]}}

    async def scenario():
        cfg = {'character': {'prompt': 'You are Rowan'}, 'provider': {'model': 'fake'},
               'allowed_channel_ids': {100, 200}, 'allowed_user_ids': set(),
               'context': {'soft_chunks': 20, 'hard_chunks': 40, 'max_fetch_messages': 1000, 'max_prompt_chars': 200000},
               'memory': {'enabled': True}}
        store, provider = Store(tmp_path, 'rowan'), Provider()
        bot = CharacterBot(cfg, store, provider)
        bot._connection.user = NS(id=9)
        try:
            for i in range(25):
                world.message(main, f'initial chat {i}', author=1 + i % 2)
            await bot.on_message(world.message(main, 'FIRST INVOCATION', invoke=True))
            first_request = payload(provider.requests[-1])
            assert len(chunks(store.previous(100).messages[:-1])) == 20
            assert first_request.count('FIRST INVOCATION') == 1
            provider.inject = True
            await bot.on_message(world.message(main, 'FOLLOWUP', invoke=True))
            await bot.on_message(world.message(main, 'ANOTHER FOLLOWUP', invoke=True))
            assert 'MESSAGE ARRIVED WHILE THINKING' in payload(provider.requests[-1])
            for i in range(65):
                world.message(main, f'absence chat {i}', author=1 + i % 2)
            await bot.on_message(world.message(main, 'BACK AFTER ABSENCE', invoke=True))
            bridge = store.previous(100)
            assert bridge.gap_before is not None
            assert len(chunks(bridge.messages, bridge.gap_before)) <= 41
            for i in range(12):
                world.message(main, f'active discussion {i}', author=2)
                await bot.on_message(world.message(main, f'active ping {i}', invoke=True))
                completed = store.previous(100)
                assert len(chunks(completed.messages, completed.gap_before)) <= 41
            assert store.previous(100).gap_before is None
            await bot.on_message(world.message(testing, 'TESTING CHANNEL ONLY', invoke=True))
            assert 'BACK AFTER ABSENCE' not in payload(provider.requests[-1])
            calls = len(provider.requests)
            await bot.on_message(world.message(main, 'BOT PING MUST NOT INVOKE', author=10, invoke=True))
            assert len(provider.requests) == calls

            # Exercise the actual quiet-period loop without a real-time wait.
            store.memory_path(100).write_text('- OLD LINE\n')
            future = store.clock() + 1801
            store.clock = lambda: future
            sleeps = []
            async def fake_sleep(delay):
                sleeps.append(delay)
                if len(sleeps) > 1:
                    raise asyncio.CancelledError
            async def ready(): pass
            monkeypatch.setattr(bot, 'wait_until_ready', ready)
            monkeypatch.setattr('bevvycord.bot.asyncio.sleep', fake_sleep)
            with pytest.raises(asyncio.CancelledError):
                await bot.memory_loop()
            assert sleeps[0] == 60
            assert store.pending_channels() == []
            assert store.memory(100).startswith('- I remember')

            # Restart the bot/store, then verify memory is preloaded in its channel.
            await bot.close()
            store.db.close()
            store = Store(tmp_path, 'rowan')
            bot = CharacterBot(cfg, store, provider)
            bot._connection.user = NS(id=9)
            await bot.on_message(world.message(main, 'AFTER RESTART', invoke=True))
            assert '<memory>\n- I remember' in provider.requests[-1][1]['content']
            assert 'TESTING CHANNEL ONLY' not in payload(provider.requests[-1])

            # Failed generations must not advance the successful checkpoint.
            prior = store.previous(100).last_response_id
            provider.fail = True
            await bot.on_message(world.message(main, 'FAILED INVOCATION', invoke=True))
            assert store.previous(100).last_response_id == prior
            archive, _, _ = store.archive(100)
            assert 'FAILED INVOCATION' not in archive
        finally:
            await bot.close()
            store.db.close()
    main, testing = Channel(100), Channel(200)
    asyncio.run(scenario())
