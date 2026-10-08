"""Keep bounded filesystem work off the gateway loop; settle writes on cancel."""
import asyncio


async def filesystem_call(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Threads cannot be cancelled. Finish the scoped operation before its
        # job is released or cleaned up; do not leave a background writer.
        await asyncio.gather(task, return_exceptions=True)
        raise
