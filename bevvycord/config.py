from pathlib import Path
import yaml

from .invite import invite_url


TOOL_DEFAULTS = {
    'enabled': False, 'sandbox_enabled': True, 'plugins': [],
    'max_steps': 12, 'max_calls': 24, 'turn_seconds': 600,
    'exec_seconds': 60, 'memory_mb': 1024, 'file_bytes': 8 * 1024 * 1024,
    'workspace_bytes': 256 * 1024 * 1024, 'workspace_files': 2000,
    'output_chars': 12000, 'max_working_chars': 300000,
    'max_artifacts': 5, 'max_parallel': 2, 'max_queue': 3, 'retention_days': 1,
}

LIBRARY_DEFAULTS = {'enabled': True, 'max_bytes': 1024 ** 3, 'max_files': 1000,
                    'file_bytes': 8 * 1024 * 1024}


def library_settings(config):
    return {**LIBRARY_DEFAULTS, **config.get('library', {})}


MEMORY_KEYS = {'enabled', 'quiet_minutes', 'cooldown_hours', 'max_wait_hours', 'retry_minutes',
               'model', 'parameters', 'max_archive_chars'}


def tool_settings(config):
    return {**TOOL_DEFAULTS, **config.get('tools', {})}


def load_config(path):
    path = Path(path).resolve()
    cfg = yaml.safe_load(path.read_text())
    if not isinstance(cfg, dict):
        raise ValueError('Configuration must be a mapping')
    for section in ('character', 'provider'):
        if not isinstance(cfg.get(section), dict):
            raise ValueError(f'{section} must be a mapping')
    for section in ('context', 'memory', 'tools', 'initiative', 'library'):
        if not isinstance(cfg.setdefault(section, {}), dict):
            raise ValueError(f'{section} must be a mapping')
    if 'application_id' in cfg:
        invite_url(cfg['application_id'])
    # YAML strings such as "false" are truthy in Python: never accidentally
    # enable DM access or paid memory jobs from a quoted boolean.
    for section, key in ((cfg, 'allow_dms'), (cfg['memory'], 'enabled'),
                         (cfg['library'], 'enabled'), (cfg['tools'], 'enabled'), (cfg['tools'], 'sandbox_enabled'), (cfg['initiative'], 'enabled')):
        if key in section and not isinstance(section[key], bool):
            raise ValueError(f'{key} must be a YAML boolean (true or false, without quotes)')
    character = cfg['character']
    if not isinstance(character.get('prompt'), str) or not character['prompt'].strip():
        raise ValueError('character.prompt is required')
    name = character.get('id', '')
    if not isinstance(name, str) or not name or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in name):
        raise ValueError('Invalid character.id')
    context = cfg.setdefault('context', {})
    context.setdefault('soft_chunks', 20)
    context.setdefault('hard_chunks', 40)
    context.setdefault('max_fetch_messages', 1000)
    context.setdefault('max_prompt_chars', 200000)
    soft, hard = context['soft_chunks'], context['hard_chunks']
    if type(soft) is not int or soft < 1 or type(hard) is not int or hard < 2 * soft:
        raise ValueError('hard_chunks must be at least twice positive soft_chunks')
    if (type(context['max_fetch_messages']) is not int or type(context['max_prompt_chars']) is not int
            or context['max_fetch_messages'] < hard + 1 or context['max_prompt_chars'] < 1):
        raise ValueError('Invalid context safety limits')
    for key in ('allowed_channel_ids', 'allowed_user_ids'):
        values = cfg.get(key, [])
        if not isinstance(values, list) or any(isinstance(i, bool) or not str(i).isdigit() or int(i) < 1 for i in values):
            raise ValueError(f'{key} must be a list of positive Discord IDs')
        cfg[key] = {int(i) for i in values}
    initiative = cfg['initiative']
    if set(initiative) - {'enabled', 'interval_minutes', 'channel_ids'}:
        raise ValueError('Unknown initiative configuration option')
    initiative.setdefault('enabled', False)
    interval = initiative.setdefault('interval_minutes', 30)
    if type(interval) not in (int, float) or not 0 < interval < float('inf'):
        raise ValueError('initiative.interval_minutes must be a positive finite number')
    channels = initiative.setdefault('channel_ids', [])
    if not isinstance(channels, list) or any(isinstance(i, bool) or not str(i).isdigit() or int(i) < 1 for i in channels):
        raise ValueError('initiative.channel_ids must be a list of positive Discord IDs')
    initiative['channel_ids'] = {int(i) for i in channels}
    if not initiative['channel_ids'].issubset(cfg['allowed_channel_ids']):
        raise ValueError('initiative channels must also be allowed channels')
    if initiative['enabled'] and (not cfg['tools'].get('enabled', False) or not channels):
        raise ValueError('Initiative requires enabled tools and explicit channel_ids')
    library = library_settings(cfg)
    if set(cfg['library']) - (LIBRARY_DEFAULTS.keys() | {'storage_dir'}):
        raise ValueError('Unknown library configuration option')
    for key in ('max_bytes', 'max_files', 'file_bytes'):
        if type(library[key]) is not int or library[key] < 1:
            raise ValueError(f'library.{key} must be a positive integer')
    if 'storage_dir' in library and (not isinstance(library['storage_dir'], str) or not library['storage_dir']):
        raise ValueError('library.storage_dir must be a nonempty path')
    provider = cfg['provider']
    if not provider.get('base_url', '').startswith(('https://', 'http://')) or not provider.get('model'):
        raise ValueError('provider.base_url and provider.model are required')
    for parameters in (provider.get('parameters', {}), cfg.get('memory', {}).get('parameters', {})):
        if not isinstance(parameters, dict) or {'messages', 'model', 'stream', 'tools', 'tool_choice'} & parameters.keys():
            raise ValueError('parameters cannot override messages, model, stream or runtime tools')
    settings = tool_settings(cfg)
    if set(cfg['tools']) - TOOL_DEFAULTS.keys():
        raise ValueError('Unknown tools configuration option')
    for key, default in TOOL_DEFAULTS.items():
        if type(default) is int and (type(settings[key]) is not int or settings[key] < 1):
            raise ValueError(f'tools.{key} must be a positive integer')
    if settings['max_artifacts'] > 10 or settings['exec_seconds'] > settings['turn_seconds']:
        raise ValueError('Invalid tool execution or artifact limits')
    plugins = settings['plugins']
    if not isinstance(plugins, list) or any(not isinstance(p, str) or not p or not all(part.isidentifier() for part in p.split('.')) for p in plugins):
        raise ValueError('tools.plugins must contain Python module names')
    unknown = set(cfg['memory']) - MEMORY_KEYS
    if unknown:
        raise ValueError(f'Unknown memory configuration option(s): {", ".join(sorted(unknown))}'
                         + (' (daily_hour_utc is no longer used; see README)' if 'daily_hour_utc' in unknown else ''))
    for key, default in (('quiet_minutes', 30), ('cooldown_hours', 4), ('max_wait_hours', 24), ('retry_minutes', 30)):
        value = cfg['memory'].setdefault(key, default)
        if type(value) not in (int, float) or not 0 < value < float('inf'):
            raise ValueError(f'memory.{key} must be a positive finite number')
    archive_limit = cfg['memory'].get('max_archive_chars', 800000)
    if type(archive_limit) is not int or archive_limit < 1:
        raise ValueError('memory.max_archive_chars must be a positive integer')
    # Relative paths are anchored to the config, never the launcher's cwd.
    cfg['storage_dir'] = str((path.parent / cfg.get('storage_dir', 'data')).resolve())
    for key in ('env_file', 'shared_env_file'):
        if cfg.get(key):
            cfg[key] = str((path.parent / cfg[key]).resolve())
    if cfg['library'].get('storage_dir'):
        cfg['library']['storage_dir'] = str((path.parent / cfg['library']['storage_dir']).resolve())
    return cfg
