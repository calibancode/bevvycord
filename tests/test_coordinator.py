import asyncio

from bevvycord.coordinator import ChannelCoordinator
from test_initiative import setup, human
from test_runtime import Scripted, response, call


def test_priority_cancellation_and_channel_independence():
    async def scenario():
        coordinator = ChannelCoordinator(pause_seconds=0)
        order = []
        async def turn(name, channel=100, initiative=True):
            async with coordinator.turn(channel, initiative):
                order.append(name)
        async with coordinator.turn(100):
            waiting = asyncio.create_task(turn('initiative'))
            cancelled = asyncio.create_task(turn('cancelled'))
            direct = asyncio.create_task(turn('direct', initiative=False))
            await asyncio.sleep(0)
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            await turn('other-channel', 200)
            assert order == ['other-channel']
        await asyncio.gather(waiting, direct)
        assert order == ['other-channel', 'direct', 'initiative']
    asyncio.run(scenario())


def test_waiting_checkin_reads_first_reply_and_reschedules_from_completion(tmp_path):
    async def scenario():
        clock = [10000]
        entered, release = asyncio.Event(), asyncio.Event()
        class First(Scripted):
            async def complete(self, *args, **kwargs):
                entered.set()
                await release.wait()
                clock[0] += 20
                return await super().complete(*args, **kwargs)
        first = First(response(call('finish', {'text': 'First character speaks.'})))
        class Second(Scripted):
            async def complete(self, *args, **kwargs):
                clock[0] += 10
                return await super().complete(*args, **kwargs)
        second = Second(response(call('finish', {})))
        a, sa, channels = setup(tmp_path / 'a', first, clock)
        b, sb, _ = setup(tmp_path / 'b', second, clock, channels)
        coordinator = ChannelCoordinator(pause_seconds=0)
        a.coordinator = b.coordinator = coordinator
        try:
            message = human(channels[100])
            await a.on_message(message)
            await b.on_message(message)
            clock[0] += 1800
            ta = asyncio.create_task(a.initiative_tick())
            await entered.wait()
            tb = asyncio.create_task(b.initiative_tick())
            await asyncio.sleep(0)
            assert not second.requests
            release.set()
            await asyncio.gather(ta, tb)
            assert 'First character speaks.' in str(second.requests)
            assert sa.attention(100)[2] == clock[0] - 10
            assert sb.attention(100)[2] == clock[0]
            clock[0] += 1799
            await b.initiative_tick()
            assert len(second.requests) == 1
        finally:
            await a.close()
            await b.close()
            sa.db.close()
            sb.db.close()
    asyncio.run(scenario())


def test_pause_delays_initiative_but_direct_turn_can_go_first():
    async def scenario():
        coordinator = ChannelCoordinator(pause_seconds=0.03)
        order = []
        async with coordinator.turn(100):
            coordinator.spoke(100)
        async def initiative():
            async with coordinator.turn(100, initiative=True):
                order.append('initiative')
        task = asyncio.create_task(initiative())
        await asyncio.sleep(0)
        async with coordinator.turn(100):
            order.append('direct')
        assert order == ['direct']
        await task
        assert order == ['direct', 'initiative']
    asyncio.run(scenario())


def test_direct_mention_overtakes_same_character_waiting_checkin(tmp_path):
    async def scenario():
        clock = [10000]
        provider = Scripted(response(call('finish', {'text': 'Direct answer.'})))
        bot, store, channels = setup(tmp_path, provider, clock)
        coordinator = ChannelCoordinator(pause_seconds=0)
        bot.coordinator = coordinator
        try:
            await bot.on_message(human(channels[100]))
            clock[0] += 1800
            async with coordinator.turn(100):
                tick = asyncio.create_task(bot.initiative_tick())
                await asyncio.sleep(0)
                direct = asyncio.create_task(bot.on_message(human(channels[100], '<@9> answer', ping=True)))
                await asyncio.sleep(0)
            await asyncio.wait_for(asyncio.gather(tick, direct), 2)
            assert len(provider.requests) == 1
            assert 'Someone is addressing you.' in str(provider.requests)
            assert store.attention(100)[:2] == (2, 2)
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())
