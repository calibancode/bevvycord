import argparse
import asyncio
import logging
import os
from pathlib import Path
import sqlite3
import signal
import sys

from .config import load_config
from .memory import rebuild_memory
from .provider import Provider
from .storage import Store
from .local import character_lock, load_env_file
from .invite import invite_url


async def run(args, cfg):
    if not args.memory_once and not os.environ.get(cfg.get('bot_token_env', 'DISCORD_BOT_TOKEN')):
        raise ValueError('Discord bot-token environment variable is unset')
    store = Store(cfg.get('storage_dir', 'data'), cfg['character']['id'],
                  retain_deleted=cfg.get('memory', {}).get('enabled', False))
    provider = None
    try:
        provider = Provider(cfg['provider'])
        if args.memory_once:
            if args.memory_once not in cfg['allowed_channel_ids']:
                raise ValueError('Channel is not allowed by this configuration')
            await rebuild_memory(store, provider, cfg, args.memory_once)
        else:
            from .bot import CharacterBot
            async with CharacterBot(cfg, store, provider) as bot:
                await bot.start(os.environ[cfg.get('bot_token_env', 'DISCORD_BOT_TOKEN')])
    finally:
        if provider:
            await provider.close()
        store.db.close()


async def interruptible_run(args, cfg):
    # add_signal_handler installs the event-loop wakeup FD, including when
    # idle. Cancel the owner so normal async context/finally cleanup can finish.
    loop, owner = asyncio.get_running_loop(), asyncio.current_task()
    previous = signal.getsignal(signal.SIGINT)
    interrupted, installed = False, False

    def interrupt():
        nonlocal interrupted
        if interrupted:
            raise KeyboardInterrupt
        interrupted = True
        owner.cancel()

    try:
        try:
            loop.add_signal_handler(signal.SIGINT, interrupt)
            installed = True
        except (NotImplementedError, RuntimeError):
            pass  # asyncio.Runner retains its default handler on other platforms.
        try:
            await run(args, cfg)
        except asyncio.CancelledError:
            if not interrupted:
                raise
            raise KeyboardInterrupt from None
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, previous)


def main():
    if sys.version_info < (3, 11):
        raise SystemExit('Bevvycord requires Python 3.11 or newer; rebuild the virtual environment with a supported interpreter.')
    parser = argparse.ArgumentParser(description='Channel-aware Discord character bot')
    parser.add_argument('--config', default='config.yaml')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--check-config', action='store_true', help='Validate without network calls or secrets')
    action.add_argument('--memory-once', type=int, metavar='CHANNEL_ID', help='Rewrite memory from scratch now (makes a provider API call)')
    action.add_argument('--jobs', action='store_true', help='List local job states in allowed channels; no credentials or network')
    action.add_argument('--activity', action='store_true', help='Inspect local turns and memory activity; no credentials or network')
    parser.add_argument('--job', help='Inspect a full job ID with --activity')
    parser.add_argument('--memory-diffs', action='store_true', help='Show recorded memory changes with --activity')
    action.add_argument('--invite', nargs='?', const='', metavar='APPLICATION_ID',
                        help='Print a join link using an explicit or configured public application ID; no keys or network')
    args = parser.parse_args()
    if (args.job or args.memory_diffs) and not args.activity:
        parser.error('--job and --memory-diffs require --activity')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    # httpx INFO includes request URLs; suppress unnecessary networking logs.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    if args.invite:
        try:
            print(invite_url(args.invite))
        except ValueError as exc:
            parser.error(str(exc))
        return
    cfg = load_config(args.config)
    if args.invite is not None:
        if not cfg.get('application_id'):
            parser.error('No application_id in this config. Pass --invite APPLICATION_ID, or start the bot to print its join link automatically.')
        print(invite_url(cfg['application_id']))
        return
    if args.check_config:
        print('Configuration valid. Channels:', sorted(cfg['allowed_channel_ids']))
        return
    if args.jobs:
        path = Path(cfg['storage_dir']) / cfg['character']['id'] / 'history.sqlite3'
        if not path.exists():
            print('No local jobs.')
            return
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
                print('No local jobs.')
                return
            for job_id, channel, actor, state, detail in db.execute('SELECT id,channel,actor,state,detail FROM jobs ORDER BY created DESC LIMIT 100'):
                if int(channel) in cfg['allowed_channel_ids']:
                    print(f'{job_id}  channel={channel} requester={actor} {state} {detail}')
        return
    if args.activity:
        from .activity import inspect_activity
        path = Path(cfg['storage_dir']) / cfg['character']['id'] / 'history.sqlite3'
        for line in inspect_activity(path, cfg['allowed_channel_ids'], args.job, memory_diffs=args.memory_diffs):
            print(line)
        return
    load_env_file(cfg.get('env_file'))
    load_env_file(cfg.get('shared_env_file'))
    try:
        with character_lock(cfg['storage_dir'], cfg['character']['id']):
            asyncio.run(interruptible_run(args, cfg))
    except KeyboardInterrupt:
        logging.info('Stopped character %s.', cfg['character']['id'])


if __name__ == '__main__':
    main()
