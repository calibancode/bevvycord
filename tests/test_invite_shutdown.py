"""Join-link paths and real SIGINT cleanup, with no live Discord connection."""
import asyncio
import logging
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace as NS
from urllib.parse import parse_qs, urlparse

import discord
import pytest
import yaml

from bevvycord.config import load_config
from bevvycord.invite import invite_url
from bevvycord.local import character_lock
from bevvycord.onboard import create_character


def test_invite_has_permissions_for_all_supported_discord_operations():
    url = urlparse(invite_url(123))
    query = parse_qs(url.query)
    assert url.scheme == 'https' and url.netloc == 'discord.com'
    assert query['client_id'] == ['123'] and query['scope'] == ['bot']
    permissions = discord.Permissions(int(query['permissions'][0]))
    required = {
        'view_channel', 'send_messages', 'embed_links', 'attach_files',
        'read_message_history', 'add_reactions',
    }
    expected = discord.Permissions.none()
    for name in required:
        setattr(expected, name, True)
    assert permissions == expected


@pytest.mark.parametrize('value', [0, -1, True, 'invalid', '123&permissions=8', ''])
def test_invite_rejects_invalid_public_ids(value):
    with pytest.raises(ValueError): invite_url(value)


def test_onboarding_persists_public_id_and_validates_before_creating_files(tmp_path):
    import shutil
    shutil.copyfile('config.example.yaml', tmp_path / 'config.example.yaml')
    with pytest.raises(ValueError):
        create_character(tmp_path, 'bad', 'Character', [100], 'test-only-token', 'test-only-key', client_id=-1)
    assert not (tmp_path / '.secrets').exists()
    cfg, _, url = create_character(tmp_path, 'rowan', 'Character', [100], 'test-only-token', 'test-only-key', client_id=123)
    assert load_config(cfg)['application_id'] == 123
    assert url == invite_url(123)


def test_cli_invite_works_without_config_or_secrets(tmp_path, monkeypatch, capsys):
    from bevvycord import __main__ as cli
    monkeypatch.setattr(sys, 'argv', ['bevvycord', '--config', str(tmp_path / 'missing.yaml'), '--invite', '123'])
    def forbidden(*args): raise AssertionError('Invite tried to load secrets or run a bot')
    monkeypatch.setattr(cli, 'load_env_file', forbidden)
    monkeypatch.setattr(cli, 'run', forbidden)
    cli.main()
    assert capsys.readouterr().out.strip() == invite_url(123)


def test_cli_invite_configured_id_and_helpful_legacy_fallback(tmp_path, monkeypatch, capsys):
    from bevvycord import __main__ as cli
    cfg = yaml.safe_load(Path('config.example.yaml').read_text())
    path = tmp_path / 'config.yaml'
    cfg['application_id'] = 123
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(sys, 'argv', ['bevvycord', '--config', str(path), '--invite'])
    def forbidden(*args): raise AssertionError('Invite tried to load secrets')
    monkeypatch.setattr(cli, 'load_env_file', forbidden)
    cli.main()
    assert capsys.readouterr().out.strip() == invite_url(123)
    del cfg['application_id']
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert 'start the bot' in capsys.readouterr().err


def test_startup_prints_invite_using_logged_in_identity(tmp_path, caplog):
    from bevvycord.bot import CharacterBot
    from bevvycord.storage import Store
    cfg = load_config('config.example.yaml')
    store = Store(tmp_path, 'rowan')
    bot = CharacterBot(cfg, store, NS())
    bot._connection.user = NS(id=123)
    async def scenario():
        try:
            with caplog.at_level(logging.INFO, logger='bevvycord.bot'):
                await bot.on_ready()
            assert invite_url(123) in caplog.text
        finally:
            await bot.close()
            store.db.close()
    asyncio.run(scenario())


def test_ctrl_c_finishes_async_cleanup_and_releases_lock_without_traceback(tmp_path):
    cfg = yaml.safe_load(Path('config.example.yaml').read_text())
    cfg['storage_dir'] = str(tmp_path / 'data')
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(cfg))
    # Exercise the actual asyncio SIGINT handler and CLI, without loading keys,
    # creating a provider or connecting Discord. The finally block must complete.
    script = '''import asyncio, signal, sys
from pathlib import Path
from bevvycord import __main__ as cli
async def local_run(args, cfg):
    print("READY", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(.02)
        Path(config_path + ".cleaned").write_text("cleaned")
config_path = sys.argv[1]
signal.signal(signal.SIGINT, signal.default_int_handler)
cli.run = local_run
sys.argv = ["bevvycord", "--config", config_path]
cli.main()
'''
    process = subprocess.Popen([sys.executable, '-u', '-c', script, str(path)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'READY'
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0
        assert 'Traceback' not in stderr and 'KeyboardInterrupt' not in stderr
        assert 'Stopped character rowan.' in stderr
        assert path.with_suffix('.yaml.cleaned').read_text() == 'cleaned'
        with character_lock(cfg['storage_dir'], 'rowan'):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def test_ctrl_c_closes_actual_bot_jobs_provider_and_memory_task(tmp_path):
    cfg = yaml.safe_load(Path('config.example.yaml').read_text())
    cfg['storage_dir'] = str(tmp_path / 'data')
    cfg['allowed_channel_ids'] = [100]
    cfg['memory']['enabled'] = True
    cfg['tools']['enabled'] = True
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(cfg))
    script = '''import asyncio, os, signal, sys
from pathlib import Path
from types import SimpleNamespace as NS
from bevvycord import __main__ as cli
from bevvycord import bot as module
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_agent_flow import Channel, Message
config_path = sys.argv[1]
os.environ["DISCORD_BOT_TOKEN"] = "test-only-token"
signal.signal(signal.SIGINT, signal.default_int_handler)
class LocalProvider:
    def __init__(self, config): self.started = asyncio.Event()
    async def complete(self, *args, **kwargs):
        self.started.set()
        await asyncio.Event().wait()
    async def close(self):
        await asyncio.sleep(.01)
        Path(config_path + ".provider-closed").write_text("closed")
async def memory_work():
    try: await asyncio.Event().wait()
    finally:
        await asyncio.sleep(.01)
        Path(config_path + ".memory-closed").write_text("closed")
class LocalBot(module.CharacterBot):
    async def start(self, token):
        self._connection.user = NS(id=9)
        self.memory_task = asyncio.create_task(memory_work())
        asyncio.create_task(self.on_message(Message(Channel(), "<@9> work")))
        await self.provider.started.wait()
        print("READY", flush=True)
        await asyncio.Event().wait()
module.CharacterBot = LocalBot
cli.Provider = LocalProvider
sys.argv = ["bevvycord", "--config", config_path]
cli.main()
'''
    process = subprocess.Popen([sys.executable, '-u', '-c', script, str(path)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'READY'
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        assert 'Traceback' not in stderr and 'KeyboardInterrupt' not in stderr
        assert Path(str(path) + '.provider-closed').exists()
        assert Path(str(path) + '.memory-closed').exists()
        import sqlite3
        with sqlite3.connect(tmp_path / 'data/rowan/history.sqlite3') as db:
            assert db.execute('SELECT state FROM jobs').fetchall() == [('cancelled',)]
            assert db.execute('SELECT count(*) FROM interactions').fetchone()[0] == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
