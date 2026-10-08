"""Offline Discord delivery through the actual character bot and tool loop."""
import asyncio
import copy
from datetime import datetime, timezone
import json
from types import SimpleNamespace as NS

import httpx
import pytest
import discord

from bevvycord.bot import CharacterBot
from bevvycord.memory import rebuild_memory
from bevvycord.storage import Store
from test_runtime import call, response, FINAL


class Channel:
    def __init__(self, channel_id=100):
        self.id, self.messages, self.files = channel_id, {}, []
        self.fail_upload = False
        self.sent_calls = []
    def typing(self):
        class Indicator:
            async def __aenter__(self): pass
            async def __aexit__(self, *args): pass
        return Indicator()
    async def history(self, before=None, limit=1000, after=None, oldest_first=False):
        count = 0
        for message in sorted(self.messages.values(), key=lambda m: m.id, reverse=not oldest_first):
            if (before is None or message.id < before.id) and (after is None or message.id > after.id):
                yield message
                count += 1
                if count >= limit:
                    break
    async def fetch_message(self, message_id): return self.messages[message_id]
    async def send(self, content='', files=(), allowed_mentions=None):
        return await self.deliver(content, files, allowed_mentions)
    async def deliver(self, content, files, allowed_mentions, reference=None):
        assert allowed_mentions.users is False and allowed_mentions.everyone is False
        self.sent_calls.append({'content': content, 'files': list(files), 'reference': reference})
        if files and self.fail_upload:
            raise RuntimeError('Simulated delivery failure')
        attachments = []
        for i, file in enumerate(files):
            data = file.fp.read()
            self.files.append((file.filename, data))
            attachments.append(NS(id=123+i, filename=file.filename, size=len(data), url='https://cdn.discordapp.com/fake-result'))
        result = Message(self, content, 9, attachments)
        result.reference = NS(message_id=reference) if reference else None
        return result


class Message:
    def __init__(self, channel, content, actor=7, attachments=()):
        self.channel, self.content = channel, content
        self.id = max(channel.messages, default=0) + 1
        self.author = NS(id=actor, display_name=f'speaker-{actor}', bot=actor == 9)
        self.guild, self.webhook_id, self.reference = NS(id=50, filesize_limit=100000), None, None
        self.type, self.created_at = discord.MessageType.default, datetime.now(timezone.utc)
        self.mentions = [NS(id=9)]
        self.components, self.embeds, self.stickers = [], [], []
        self.attachments = list(attachments)
        self.reactions = []
        self.added_reactions = []
        channel.messages[self.id] = self
    async def add_reaction(self, emoji):
        self.added_reactions.append(emoji)
    async def reply(self, content='', file=None, files=(), mention_author=False, allowed_mentions=None):
        assert allowed_mentions.users is False and allowed_mentions.everyone is False
        assert not mention_author
        return await self.channel.deliver(content, [file] if file else files, allowed_mentions, self.id)


def config():
    return {'character': {'prompt': 'You are Rowan'}, 'provider': {'model': 'fake'},
            'allowed_channel_ids': {100, 200}, 'allowed_user_ids': set(),
            'context': {'soft_chunks': 20, 'hard_chunks': 40, 'max_fetch_messages': 1000, 'max_prompt_chars': 200000},
            'memory': {'enabled': True}, 'tools': {'enabled': True}}


def run_file_memory_flow(tmp_path, monkeypatch, real_exec=False, fail_delivery=False):
    channel, requests = Channel(), []
    channel.fail_upload = fail_delivery
    attachment = NS(id=12, filename='input.txt', size=6, url='https://cdn.discordapp.com/fake-input')
    trigger = Message(channel, '<@9> uppercase this and remember I prefer text files', attachments=[attachment])
    original_client = httpx.AsyncClient
    def transport(request):
        assert str(request.url) == attachment.url
        return httpx.Response(200, content=b'hello\n')
    monkeypatch.setattr('bevvycord.bot.httpx.AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(transport), **kwargs))

    class Provider:
        async def complete(self, messages, **kwargs):
            requests.append(copy.deepcopy(messages))
            step = len(requests)
            if step == 1:
                assert '1:12' in messages[-1]['content']
                return response(call('get_attachment', {'attachment_id': '1:12'}))
            if step == 2:
                path = json.loads(messages[-1]['content'])['path']
                if real_exec:
                    # Use JSON-quoted path in Python and shell-safe double quoting.
                    command = 'python3 - <<\'CODE\'\nfrom pathlib import Path\nPath("output.txt").write_text(Path(' + repr(path) + ').read_text().upper())\nCODE'
                    return response(call('exec', {'command': command}, 'transform'))
                return response(call('write_file', {'path': 'output.txt', 'content': 'HELLO\n'}, 'transform'))
            if step == 3:
                if real_exec: assert json.loads(messages[-1]['content'])['exit_code'] == 0
                return response(call('read_file', {'path': 'output.txt'}, 'verify'))
            if step == 4:
                assert json.loads(messages[-1]['content'])['content'] == 'HELLO\n'
                return response(call('return_file', {'path': 'output.txt'}, 'stage'),
                                call('remember', {'note': 'Bevvy prefers text files', 'message_ids': [trigger.id]}, 'memory'))
            if step == 5: return FINAL
            if '<turn_error>' in messages[-1]['content']:
                assert kwargs['tool_choice'] == 'none'
                return {'finish_reason': 'stop', 'message': {'content': 'Couldn’t upload the file.'}}
            assert all(m['role'] in ('system', 'user') for m in messages)
            assert 'PRIVATE INTERNAL REASONING' not in str(messages)
            return FINAL
        async def generate(self, messages, **kwargs):
            assert 'OLD MEMORY' in str(messages)
            assert 'Bevvy prefers text files' in str(messages)
            return '- Bevvy prefers text files.'

    async def scenario():
        store, cfg, provider = Store(tmp_path, 'rowan'), config(), Provider()
        store.memory_path(100).write_text('OLD MEMORY')
        bot = CharacterBot(cfg, store, provider)
        bot._connection.user = NS(id=9)
        try:
            await bot.on_message(trigger)
            assert len(requests) == (6 if fail_delivery else 5)
            states = store.db.execute('SELECT state FROM jobs').fetchall()
            if fail_delivery:
                assert states == [('failed',)]
                assert not store.previous(100)
                deliveries = store.db.execute('SELECT state FROM deliveries ORDER BY part').fetchall()
                assert deliveries == [('started',), ('sent',)]
                assert channel.sent_calls[-1]['content'] == 'Couldn’t upload the file.'
                assert not channel.sent_calls[-1]['files'] and channel.sent_calls[-1]['reference'] is None
                assert all(f.fp.closed for call in channel.sent_calls for f in call['files'])
                assert store.has_pending_notes(100)
                assert store.due_memory_channels(cfg['memory']) == ['100']
                return
            assert states == [('complete',)]
            assert channel.files == [('output.txt', b'HELLO\n')]
            assert [m.content for m in channel.messages.values() if m.author.bot] == ['Done.']
            assert len(channel.sent_calls) == 1
            assert channel.sent_calls[0]['reference'] == trigger.id
            assert len(channel.sent_calls[0]['files']) == 1
            assert channel.sent_calls[0]['files'][0].fp.closed
            archive, _, _ = store.archive(100)
            assert 'PRIVATE INTERNAL REASONING' not in archive
            assert 'output.txt' in archive
            assert 'get_attachment' not in archive
            assert store.due_memory_channels(cfg['memory']) == ['100']
            await rebuild_memory(store, provider, cfg, 100)
            await bot.on_message(Message(channel, '<@9> next ordinary reply'))
            assert any('<memory>\n- Bevvy prefers text files.' in m['content'] for m in requests[-1])
            assert 'PRIVATE INTERNAL REASONING' not in str(requests[-1])
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_file_delivery_memory_and_next_turn(tmp_path, monkeypatch):
    run_file_memory_flow(tmp_path, monkeypatch)


def test_delivery_failure_preserves_memory_and_receipts(tmp_path, monkeypatch):
    run_file_memory_flow(tmp_path, monkeypatch, fail_delivery=True)


def test_owner_cancel_status_and_bounded_queue(tmp_path):
    class Provider:
        def __init__(self): self.started = asyncio.Event()
        async def complete(self, messages, **kwargs):
            self.started.set()
            await asyncio.Event().wait()
    async def scenario():
        cfg = config()
        cfg['tools']['max_queue'] = 1
        store, provider, channel = Store(tmp_path, 'rowan'), Provider(), Channel()
        bot = CharacterBot(cfg, store, provider)
        bot._connection.user = NS(id=9)
        first = asyncio.create_task(bot.on_message(Message(channel, '<@9> start')))
        try:
            await provider.started.wait()
            await bot.on_message(Message(channel, '<@9> status'))
            assert 'is running' in list(channel.messages.values())[-1].content
            await bot.on_message(Message(channel, '<@9> cancel', actor=8))
            assert not first.done()
            second = asyncio.create_task(bot.on_message(Message(channel, '<@9> queued')))
            await asyncio.sleep(0)
            await bot.on_message(Message(channel, '<@9> too many'))
            assert 'queue here is full' in list(channel.messages.values())[-1].content
            second.cancel()
            with pytest.raises(asyncio.CancelledError): await second
            await bot.on_message(Message(channel, '<@9> cancel'))
            with pytest.raises(asyncio.CancelledError): await first
            assert store.db.execute('SELECT state FROM jobs').fetchone()[0] == 'cancelled'
            assert not bot.active and not bot.tasks
            assert bot.waiting[100] == 0
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_turn_timeout_includes_context_preparation(tmp_path, monkeypatch):
    async def scenario():
        cfg, store, channel = config(), Store(tmp_path, 'rowan'), Channel()
        cfg['tools']['turn_seconds'] = 1
        bot = CharacterBot(cfg, store, NS())
        bot._connection.user = NS(id=9)
        async def hang(trigger): await asyncio.Event().wait()
        monkeypatch.setattr(bot, 'history', hang)
        try:
            await bot.on_message(Message(channel, '<@9> start'))
            assert 'couldn’t complete' in list(channel.messages.values())[-1].content
            assert not bot.active and not bot.tasks
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())
