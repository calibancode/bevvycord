from datetime import datetime, timezone

from .prompts import IDENTITY_INSTRUCTIONS, MEMORY_UPDATE_INSTRUCTIONS, MEMORY_INSTRUCTIONS


def _system(store, config, channel, instructions):
    identity = store.speaker_id(channel)
    identity_note = f'\n\nYour Discord speaker ID is bot:{identity}.' if identity else ''
    return config['character']['prompt'].strip() + identity_note + '\n\n' + instructions + '\n\n' + IDENTITY_INSTRUCTIONS


def _model(config):
    settings = config.get('memory', {})
    return settings.get('model', config['provider']['model']), settings.get('parameters', {})


def _enabled(config):
    settings = config.get('memory', {})
    if not settings.get('enabled', False):
        raise ValueError('Memory is disabled')
    return settings


async def rebuild_memory(store, provider, config, channel):
    from .activity import current, observe
    if current.get() is not None and current.get()['kind'] == 'memory':
        return await _rebuild_memory(store, provider, config, channel)
    return await observe(store, channel, 'memory', lambda: _rebuild_memory(store, provider, config, channel))


async def _rebuild_memory(store, provider, config, channel):
    """Forced refresh: rewrite MEMORY.md from scratch using the full archive,
    standing remember/forget requests and the current MEMORY.md."""
    settings = _enabled(config)
    through = store.clock()
    archive, checkpoint, note_ids = store.archive(channel, include_retractions=True)
    current = store.memory(channel).strip()
    if not checkpoint and not note_ids and not current:
        return False
    source = (f'Current date (UTC): {datetime.now(timezone.utc).date()}\n'
              f'<current_memory>\n{current or "(empty)"}\n</current_memory>\n\n'
              f'<participation_archive>\n{archive}\n</participation_archive>')
    if len(source) > settings.get('max_archive_chars', 800000):
        raise ValueError('Participation archive exceeds memory.max_archive_chars; rebuild skipped without replacing memory')
    model, parameters = _model(config)
    result = await provider.generate([
        {'role': 'system', 'content': _system(store, config, channel, MEMORY_INSTRUCTIONS)},
        {'role': 'user', 'content': source},
    ], model=model, parameters=parameters)
    _validate_document(result, settings.get('max_archive_chars', 800000))
    store.write_memory(channel, result, checkpoint, note_ids, through)
    return True


async def update_memory(store, provider, config, channel):
    from .activity import observe
    return await observe(store, channel, 'memory', lambda: _update_memory(store, provider, config, channel))


async def _update_memory(store, provider, config, channel):
    """Scheduled update: rewrite MEMORY.md with what changed since
    the last run. Without an existing memory, fall back to a full refresh."""
    settings = _enabled(config)
    current = store.memory(channel)
    if not current.strip() or store.memory_needs_rebuild(channel):
        return await rebuild_memory(store, provider, config, channel)
    through = store.clock()
    changes, checkpoint, note_ids = store.changes(channel)
    if not changes:
        store.write_memory(channel, current, checkpoint, note_ids, through)
        return False
    limit = settings.get('max_archive_chars', 800000)
    source = (f'Current date (UTC): {datetime.now(timezone.utc).date()}\n'
              f'<current_memory>\n{current.strip()}\n</current_memory>\n\n{changes}')
    if len(source) > limit:
        raise ValueError('Memory update input exceeds memory.max_archive_chars; update skipped without replacing memory')
    model, parameters = _model(config)
    result = await provider.generate([
        {'role': 'system', 'content': _system(store, config, channel, MEMORY_UPDATE_INSTRUCTIONS)},
        {'role': 'user', 'content': source},
    ], model=model, parameters=parameters)
    _validate_document(result, limit)
    store.write_memory(channel, result, checkpoint, note_ids, through)
    return result.rstrip() != current.rstrip()


def _validate_document(result, limit):
    if not isinstance(result, str) or not result.strip():
        raise ValueError('Memory writer returned an empty document; existing memory retained')
    if result.lstrip().startswith('```'):
        raise ValueError('Memory writer returned a fenced document; existing memory retained')
    if len(result) > limit:
        raise ValueError('Written memory exceeds memory.max_archive_chars; existing memory retained')
