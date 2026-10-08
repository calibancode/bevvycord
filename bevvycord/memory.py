from datetime import datetime, timezone
import json

from .prompts import IDENTITY_INSTRUCTIONS, MEMORY_EDIT_INSTRUCTIONS, MEMORY_INSTRUCTIONS

EDIT_STEPS = 12


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
    """Forced refresh: rewrite MEMORY.md from scratch using the full archive,
    standing remember/forget requests and the current MEMORY.md."""
    settings = _enabled(config)
    through = store.clock()
    archive, checkpoint, note_ids = store.archive(channel)
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
    if result.lstrip().startswith('```'):
        raise ValueError('Memory writer returned a fenced document; existing memory retained')
    store.write_memory(channel, result, checkpoint, note_ids, through)
    return True


def _tool(name, description, properties):
    return {'type': 'function', 'function': {'name': name, 'description': description, 'parameters': {
        'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}}}


EDIT_TOOLS = [
    _tool('replace', 'Replace text that occurs exactly once in MEMORY.md. Use an empty "new" to delete it.',
          {'old': {'type': 'string', 'minLength': 1}, 'new': {'type': 'string'}}),
    _tool('append', 'Append one or more lines to the end of MEMORY.md.', {'text': {'type': 'string', 'minLength': 1}}),
]


def apply_edit(draft, name, args):
    """Returns (new_draft, result) without raising on model mistakes."""
    if name == 'replace' and isinstance(args.get('old'), str) and isinstance(args.get('new'), str) and args['old']:
        count = draft.count(args['old'])
        if count != 1:
            return draft, {'error': f'"old" occurs {count} times; it must match exactly once'}
        return draft.replace(args['old'], args['new']), {'status': 'replaced'}
    if name == 'append' and isinstance(args.get('text'), str) and args['text'].strip():
        return draft.rstrip('\n') + ('\n' if draft.strip() else '') + args['text'].strip('\n') + '\n', {'status': 'appended'}
    return draft, {'error': 'Malformed edit'}


async def update_memory(store, provider, config, channel):
    """Scheduled update: edit the existing MEMORY.md with what changed since
    the last run. Without an existing memory, fall back to a full refresh."""
    settings = _enabled(config)
    current = store.memory(channel)
    if not current.strip():
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
    scratch = [{'role': 'system', 'content': _system(store, config, channel, MEMORY_EDIT_INSTRUCTIONS)},
               {'role': 'user', 'content': source}]
    draft = current
    for _ in range(EDIT_STEPS):
        choice = await provider.complete(scratch, model=model, parameters=parameters, tools=EDIT_TOOLS)
        raw = choice.get('message', {})
        calls = raw.get('tool_calls')
        if not calls:
            if choice.get('finish_reason') not in ('stop', 'end_turn'):
                raise RuntimeError('Memory writer did not finish')
            if len(draft) > limit:
                raise ValueError('Edited memory exceeds memory.max_archive_chars; existing memory retained')
            store.write_memory(channel, draft, checkpoint, note_ids, through)
            return True
        # Preserve DeepSeek reasoning_content verbatim on assistant replay.
        scratch.append({**{k: v for k, v in raw.items() if k in ('content', 'tool_calls', 'reasoning_content')},
                        'role': 'assistant'})
        for call in calls:
            function = call.get('function', {}) if isinstance(call, dict) else {}
            try:
                args = json.loads(function.get('arguments') or '')
            except (ValueError, TypeError):
                args = None
            if isinstance(args, dict):
                draft, result = apply_edit(draft, function.get('name'), args)
            else:
                result = {'error': 'Arguments must be a JSON object'}
            scratch.append({'role': 'tool', 'tool_call_id': call.get('id') if isinstance(call, dict) else None,
                            'content': json.dumps(result)})
    raise RuntimeError('Memory writer step limit reached; existing memory retained')
