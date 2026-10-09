import asyncio
from types import SimpleNamespace as NS

from bevvycord.bot import CharacterBot
from bevvycord.storage import Store
from test_agent_flow import Channel, Message, config


def test_silent_rebuild_bypasses_conversation_and_other_characters(tmp_path):
    async def scenario():
        cfg = config()
        cfg['memory']['enabled'] = True
        cfg['tools']['enabled'] = False
        cfg['initiative'] = {'enabled': True, 'channel_ids': {100}}
        store = Store(tmp_path, 'rowan')
        bot = CharacterBot(cfg, store, None)
        bot._connection.user = NS(id=9)
        channel = Channel()
        try:
            command = Message(channel, '<@9> memory rebuild', 7)
            await bot.on_message(command)
            assert store.memory_needs_rebuild(100)
            assert store.due_memory_channels({}) == ['100']
            assert store.attention(100) is None
            assert not channel.sent_calls and not bot.tasks and not bot.waiting
            assert store.previous(100) is None
            assert not bot.message_activity(command)
            # Even a command addressed to a different character is not activity.
            other = Message(channel, '<@10> memory rebuild', 7)
            await bot.on_message(other)
            assert not bot.message_activity(other)
            assert store.attention(100) is None
            trigger = Message(channel, 'Hello', 7)
            history = await bot.history(trigger)
            assert all('memory rebuild' not in entry.text for entry in history)
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_rebuild_control_obeys_authorization_and_memory_enabled(tmp_path):
    async def scenario():
        cfg = config()
        cfg['allowed_user_ids'] = {7}
        cfg['memory']['enabled'] = True
        store = Store(tmp_path, 'rowan')
        bot = CharacterBot(cfg, store, None)
        bot._connection.user = NS(id=9)
        try:
            for actor, channel in ((8, Channel()), (9, Channel()), (7, Channel(300))):
                await bot.on_message(Message(channel, '<@9> memory rebuild', actor))
            assert store.pending_channels() == []
            cfg['memory']['enabled'] = False
            await bot.on_message(Message(Channel(), '<@9> memory rebuild', 7))
            assert store.pending_channels() == []
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_checkin_history_excludes_latest_rebuild_command(tmp_path):
    from test_initiative import setup, human
    from test_runtime import Scripted, call, response
    async def scenario():
        clock = [10000]
        provider = Scripted(response(call('finish', {})))
        bot, store, channels = setup(tmp_path, provider, clock)
        bot.config['memory']['enabled'] = True
        try:
            await bot.on_message(human(channels[100], 'Something worth remembering'))
            await bot.on_message(human(channels[100], '<@9> memory rebuild', ping=True))
            clock[0] += 1800
            await bot.initiative_tick()
            assert len(provider.requests) == 1
            assert 'memory rebuild' not in str(provider.requests)
            assert not channels[100].sent_calls
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())
