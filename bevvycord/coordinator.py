"""Shared conversation turns; each Discord client retains its own state."""
import asyncio
from contextlib import asynccontextmanager


class ChannelCoordinator:
    def __init__(self, pause_seconds=15):
        self.pause_seconds = pause_seconds
        self.channels = {}

    @asynccontextmanager
    async def turn(self, channel, initiative=False):
        state = self.channels.setdefault(channel, {
            'condition': asyncio.Condition(), 'queue': [], 'busy': False, 'ready': 0})
        condition = state['condition']
        ticket = (int(initiative), object())
        async with condition:
            state['queue'].append(ticket)
            try:
                while True:
                    first = min(state['queue'], key=lambda item: item[0])
                    delay = state['ready'] - asyncio.get_running_loop().time() if initiative else 0
                    if not state['busy'] and first is ticket and delay <= 0:
                        state['queue'].remove(ticket)
                        state['busy'] = True
                        break
                    if not state['busy'] and first is ticket and delay > 0:
                        try:
                            await asyncio.wait_for(condition.wait(), delay)
                        except TimeoutError:
                            pass
                    else:
                        await condition.wait()
            except BaseException:
                state['queue'].remove(ticket)
                condition.notify_all()
                raise
        try:
            yield
        finally:
            async with condition:
                state['busy'] = False
                condition.notify_all()

    def spoke(self, channel):
        state = self.channels.setdefault(channel, {
            'condition': asyncio.Condition(), 'queue': [], 'busy': False, 'ready': 0})
        state['ready'] = asyncio.get_running_loop().time() + self.pause_seconds
