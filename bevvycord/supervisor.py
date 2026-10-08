"""Run independent character clients under one channel coordinator."""
import asyncio
import logging
import math
import os
from pathlib import Path

import yaml

from .config import load_config
from .coordinator import ChannelCoordinator
from .local import character_lock, read_env_file
from .provider import Provider
from .storage import Store

log = logging.getLogger(__name__)


def load_supervisor(path):
    path = Path(path).resolve()
    manifest = yaml.safe_load(path.read_text())
    if not isinstance(manifest, dict) or set(manifest) - {'characters', 'pause_seconds'}:
        raise ValueError('Supervisor requires characters and optional pause_seconds')
    pause = manifest.get('pause_seconds', 15)
    if type(pause) not in (int, float) or not math.isfinite(pause) or pause < 0:
        raise ValueError('pause_seconds must be a finite nonnegative number')
    entries = manifest.get('characters')
    if not isinstance(entries, list) or not entries:
        raise ValueError('characters must be a nonempty list')
    configs, ids = [], set()
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) - {'config', 'enabled'}
                or not isinstance(entry.get('config'), str)
                or type(entry.get('enabled', True)) is not bool):
            raise ValueError('Each character requires config and optional boolean enabled')
        if not entry.get('enabled', True):
            continue
        cfg = load_config(path.parent / entry['config'])
        name = cfg['character']['id']
        if name in ids:
            raise ValueError(f'Duplicate character: {name}')
        ids.add(name)
        configs.append(cfg)
    return configs, pause


def character_environment(cfg):
    # Explicit process overrides win; character files never leak into siblings.
    return {**read_env_file(cfg.get('shared_env_file')),
            **read_env_file(cfg.get('env_file')), **os.environ}


async def run_supervisor(configs, pause):
    from .bot import CharacterBot
    coordinator = ChannelCoordinator(pause)

    async def character(cfg):
        name = cfg['character']['id']
        with character_lock(cfg['storage_dir'], name):
            environment = character_environment(cfg)
            token = environment.get(cfg.get('bot_token_env', 'DISCORD_BOT_TOKEN'))
            if not token:
                raise ValueError('Discord bot-token environment variable is unset')
            store = Store(cfg['storage_dir'], name, retain_deleted=cfg['memory'].get('enabled', False))
            provider = None
            try:
                provider = Provider(cfg['provider'], environment=environment)
                async with CharacterBot(cfg, store, provider, coordinator) as bot:
                    await bot.start(token)
            finally:
                if provider:
                    await provider.close()
                store.db.close()

    async def isolated(cfg):
        try:
            await character(cfg)
        except Exception as exc:
            # Do not include exceptions that could contain credentials.
            log.error('Character %s stopped (%s); other characters continue.', cfg['character']['id'], type(exc).__name__)

    tasks = [asyncio.create_task(isolated(cfg)) for cfg in configs]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
