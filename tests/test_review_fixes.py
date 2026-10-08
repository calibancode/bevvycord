import asyncio
import threading
from types import SimpleNamespace as NS

import discord
import pytest

from bevvycord.bot import CharacterBot
from bevvycord.filesystem import filesystem_call
from bevvycord.storage import Store
from test_agent_flow import Channel, Message, config
from test_runtime import Scripted, FINAL, job


def make_bot(tmp_path, provider=None):
    cfg = config()
    cfg['memory']['enabled'] = False
    cfg['context'].update(soft_chunks=2, hard_chunks=4, max_fetch_messages=8)
    store = Store(tmp_path, 'review')
    bot = CharacterBot(cfg, store, provider or Scripted(FINAL))
    bot._connection.user = NS(id=9)
    return bot, store


@pytest.mark.parametrize('resolved', ['human', 'deleted', 'uncached'])
def test_reply_fallback_fetches_only_unresolved_references(tmp_path, resolved):
    async def scenario():
        bot, store = make_bot(tmp_path)
        channel = Channel()
        source = Message(channel, 'another human', actor=8)
        trigger = Message(channel, 'reply', actor=7)
        trigger.mentions = []
        reference = source if resolved == 'human' else NS(deleted=True) if resolved == 'deleted' else None
        trigger.reference = NS(message_id=source.id, resolved=reference)
        fetches = []
        original = channel.fetch_message
        async def fetch(message_id):
            fetches.append(message_id)
            return await original(message_id)
        channel.fetch_message = fetch
        try:
            await bot.on_message(trigger)
            assert fetches == ([source.id] if resolved == 'uncached' else [])
            assert not channel.sent_calls
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('count,limited', [(7, False), (8, False), (9, True), (30, True)])
def test_single_speaker_history_has_bounded_context_and_honest_limit_note(tmp_path, count, limited):
    async def scenario():
        provider = Scripted(FINAL)
        bot, store = make_bot(tmp_path, provider)
        channel = Channel()
        for _ in range(count):
            trigger = Message(channel, 'long monologue')
        try:
            await bot.on_message(trigger)
            assert len(provider.requests) == 1
            assert ('channel-history fetch limit' in str(provider.requests[0])) == limited
            saved = store.previous(100)
            assert saved.history_limited == limited
            assert len(saved.messages) == min(count, 8) + 1
            assert sum(m.id == trigger.id for m in saved.messages) == 1
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_system_message_spam_does_not_prevent_a_reply_and_limited_reads_do_not_accumulate(tmp_path):
    async def scenario():
        provider = Scripted(FINAL, FINAL)
        bot, store = make_bot(tmp_path, provider)
        channel = Channel()
        try:
            for _ in range(20):
                system = Message(channel, 'pin notification')
                system.type = discord.MessageType.pins_add
            trigger = Message(channel, 'hello')
            await bot.on_message(trigger)
            assert len(store.previous(100).messages) == 2
            for _ in range(20):
                trigger = Message(channel, 'more monologue')
            await bot.on_message(trigger)
            assert len(store.previous(100).messages) == 9
            assert 'pin notification' not in str(provider.requests)
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_storage_scan_does_not_block_gateway_loop(tmp_path):
    async def scenario():
        current, registry = job(tmp_path)
        entered, release = threading.Event(), threading.Event()
        main_thread = threading.get_ident()
        original = current.check_storage
        def slow_scan(*args):
            assert threading.get_ident() != main_thread
            entered.set()
            assert release.wait(2)
            original(*args)
        current.check_storage = slow_scan
        task = asyncio.create_task(registry.call('write_file', {'path': 'result.txt', 'content': 'ok'}, current))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            # The scan is blocked, yet the loop can process another event.
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        assert (await task)['bytes'] == 2
        assert current.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
        current.store.db.close()
    asyncio.run(scenario())


def test_cancelled_filesystem_write_settles_before_job_is_released(tmp_path):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        def write():
            entered.set()
            assert release.wait(2)
            (tmp_path / 'result.txt').write_text('settled')
        task = asyncio.create_task(filesystem_call(write))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (tmp_path / 'result.txt').read_text() == 'settled'
    asyncio.run(scenario())
