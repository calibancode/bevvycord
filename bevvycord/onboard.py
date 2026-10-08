"""Interactive local character setup. No Discord or provider requests."""
from getpass import getpass
from pathlib import Path
import os
import re
import shlex
import sys
import webbrowser

import yaml

from .invite import invite_url

ROOT = Path(__file__).resolve().parent.parent


def write_private(path, text, mode=0o600):
    # Never overwrite an existing character or follow an existing symlink.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write(text)


def create_character(root, character, prompt, channels, token, key=None, shared_key=False,
                     model='deepseek-flash', client_id=None, memory=False, memory_model='deepseek-flash', tools=False,
                     initiative_channels=()):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', character):
        raise ValueError('Use a character ID beginning with a letter, followed by letters, digits, _ or -')
    if not prompt.strip() or not channels or any(type(i) is not int or i <= 0 for i in channels):
        raise ValueError('A character prompt and at least one positive channel ID are required')
    if any(type(i) is not int or i <= 0 or i not in channels for i in initiative_channels):
        raise ValueError('Check-in channel IDs must be positive IDs from the allowed channels')
    if initiative_channels and not tools:
        raise ValueError('Check-ins require tool turns for reactions and silence')
    if not token.strip() or (not shared_key and (not key or not key.strip())):
        raise ValueError('Discord token and provider key are required')
    if any('\n' in value or '\r' in value for value in (token, key or '')):
        raise ValueError('Keys must be single-line values')
    invite = invite_url(client_id) if client_id is not None else None
    root = Path(root)
    configs, secrets, launchers = root / 'characters', root / '.secrets', root / 'bin'
    for directory in (configs, secrets, launchers):
        directory.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets, 0o700)
    config_path, env_path = configs / f'{character}.yaml', secrets / f'{character}.env'
    launcher = launchers / f'run-{character}'
    if any(p.exists() or p.is_symlink() for p in (config_path, env_path, launcher)):
        raise ValueError('That character already has setup files; choose a new ID or edit the existing files')
    env_prefix = character.upper().replace('-', '_')
    # Separate env files make even normalization collisions harmless per process.
    bot_var = f'{env_prefix}_DISCORD_TOKEN'
    key_var = 'DEEPSEEK_API_KEY' if shared_key else f'{env_prefix}_DEEPSEEK_KEY'
    cfg = yaml.safe_load((root / 'config.example.yaml').read_text())
    cfg['character'] = {'id': character, 'prompt': prompt.strip()}
    cfg['bot_token_env'] = bot_var
    if client_id is not None:
        cfg['application_id'] = int(client_id)
    cfg['env_file'] = f'../.secrets/{character}.env'
    cfg['storage_dir'] = '../data'
    cfg['allowed_channel_ids'] = list(dict.fromkeys(channels))
    cfg['provider']['api_key_env'], cfg['provider']['model'] = key_var, model
    cfg['memory']['enabled'], cfg['memory']['model'] = memory, memory_model
    cfg.setdefault('tools', {})['enabled'] = tools
    cfg.setdefault('initiative', {}).update(enabled=bool(initiative_channels),
                                          channel_ids=list(dict.fromkeys(initiative_channels)))
    lines = [f'{bot_var}={shlex.quote(token.strip())}']
    if shared_key:
        shared_path = secrets / 'global.env'
        if not shared_path.exists():
            if not key or not key.strip():
                raise ValueError('A shared provider key is required for the first character using it')
            write_private(shared_path, f'DEEPSEEK_API_KEY={shlex.quote(key.strip())}\n')
        # The character file links shared credentials through configuration,
        # without duplicating the global key into every character's file.
        cfg['shared_env_file'] = '../.secrets/global.env'
    else:
        lines.append(f'{key_var}={shlex.quote(key.strip())}')
    write_private(env_path, '\n'.join(lines) + '\n')
    write_private(config_path, yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
    script = ('#!/usr/bin/env bash\nset -euo pipefail\n'
              'project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"\n'
              'cd -- "$project_dir"\n'
              f'exec "$project_dir/.venv/bin/python" -m bevvycord --config '
              f'"$project_dir/characters/{character}.yaml" "$@"\n')
    write_private(launcher, script, 0o700)
    return config_path, launcher, invite


def main():
    if not sys.stdin.isatty():
        raise SystemExit('Run onboarding in an interactive terminal; keys are entered with hidden prompts.')
    print('Local character setup — no bot starts and no API requests are made.')
    print('Create one application per character: https://discord.com/developers/applications')
    print('Under Bot, enable Message Content Intent and obtain the bot token.')
    if input('Open the developer portal in your browser? [y/N] ').strip().lower() == 'y':
        webbrowser.open('https://discord.com/developers/applications')
    character = input('Character ID (e.g. rowan): ').strip()
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', character):
        raise SystemExit('Invalid character ID.')
    if any(p.exists() or p.is_symlink() for p in (
        ROOT / 'characters' / f'{character}.yaml', ROOT / '.secrets' / f'{character}.env',
        ROOT / 'bin' / f'run-{character}',
    )):
        raise SystemExit('That character already has setup files; edit those files or choose another ID.')
    prompt_file = input('Character prompt file path (blank to enter a short prompt): ').strip()
    prompt = Path(prompt_file).expanduser().read_text() if prompt_file else input('Character prompt: ')
    try:
        channels = [int(i.strip()) for i in input('Allowed channel IDs, comma-separated (include testing channel): ').split(',')]
    except ValueError:
        raise SystemExit('Channel IDs must be comma-separated numbers.') from None
    client_id = input('Discord application/client ID (optional; startup also prints a join link): ').strip()
    if client_id:
        try:
            invite_url(client_id)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
    model = input('Reply model: deepseek-flash or deepseek-v4-pro [deepseek-flash]: ').strip() or 'deepseek-flash'
    shared = input('Use a shared DeepSeek key instead of a character key? [y/N] ').strip().lower() == 'y'
    token = getpass('Discord bot token (hidden): ')
    key = None
    if not shared or not (ROOT / '.secrets/global.env').exists():
        key = getpass('DeepSeek API key (hidden): ')
    memory = input('Enable in-character memory writing after quiet periods? [y/N] ').strip().lower() == 'y'
    memory_model = (input('Memory writer model [deepseek-flash]: ').strip() or 'deepseek-flash') if memory else 'deepseek-flash'
    tools = input('Enable tool turns and isolated file work (Linux bubblewrap required)? [y/N] ').strip().lower() == 'y'
    try:
        checkin = input('Optional 30-minute check-in channel IDs (comma-separated; blank disables; leave testing out): ').strip() if tools else ''
        initiative_channels = [int(i.strip()) for i in checkin.split(',')] if checkin else []
        config, launcher, invite = create_character(ROOT, character, prompt, channels, token, key, shared, model, client_id or None, memory, memory_model, tools, initiative_channels)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(f'Created config: {config}')
    if invite:
        print(f'Invite your bot: {invite}')
        print(f'Print this link again: {launcher} --invite')
    else:
        print('Your bot will print its join link when it connects to Discord.')
    print('Restrict channel permissions in Discord to match the allowed channels.')
    print(f'Check setup: {launcher} --check-config')
    print(f'Run when ready: {launcher}')


if __name__ == '__main__':
    main()
