import os
from pathlib import Path
import shutil
import subprocess

import pytest

from bevvycord.config import load_config
from bevvycord.local import character_lock, load_env_file
from bevvycord.onboard import create_character


def project(tmp_path):
    shutil.copyfile('config.example.yaml', tmp_path / 'config.example.yaml')
    return tmp_path


def test_onboard_private_credentials_paths_and_safe_roundtrip(tmp_path, monkeypatch):
    root = project(tmp_path)
    token = 'fake-token with $() and `quotes`'
    key = "fake-key'with spaces"
    config, launcher, invite = create_character(root, 'rowan', 'Be Rowan.', [100, 200], token, key, client_id=123)
    assert config.stat().st_mode & 0o777 == 0o600
    assert (root / '.secrets/rowan.env').stat().st_mode & 0o777 == 0o600
    assert (root / '.secrets').stat().st_mode & 0o777 == 0o700
    assert token not in config.read_text() and key not in launcher.read_text()
    subprocess.run(['bash', '-n', str(launcher)], check=True)
    monkeypatch.chdir('/tmp')
    cfg = load_config(config)
    assert cfg['storage_dir'] == str(root / 'data')
    monkeypatch.delenv('ROWAN_DISCORD_TOKEN', raising=False)
    monkeypatch.delenv('ROWAN_DEEPSEEK_KEY', raising=False)
    load_env_file(cfg['env_file'])
    assert os.environ['ROWAN_DISCORD_TOKEN'] == token
    assert os.environ['ROWAN_DEEPSEEK_KEY'] == key
    assert 'client_id=123' in invite and 'scope=bot' in invite
    with pytest.raises(ValueError, match='already has setup files'):
        create_character(root, 'rowan', 'Other prompt', [200], 'other', 'other')


def test_shared_key_link_and_process_environment_override(tmp_path, monkeypatch):
    root = project(tmp_path)
    rowan, _, _ = create_character(root, 'rowan', 'Rowan', [100], 'token-a', 'shared-key', shared_key=True, model='deepseek-v4-pro', memory=True)
    rowan_cfg = load_config(rowan)
    assert rowan_cfg['provider']['model'] == 'deepseek-v4-pro'
    assert rowan_cfg['memory']['model'] == 'deepseek-flash'
    mira, _, _ = create_character(root, 'mira', 'Mira', [100], 'token-b', shared_key=True)
    assert 'shared-key' not in (root / '.secrets/mira.env').read_text()
    assert 'shared-key' not in (root / '.secrets/rowan.env').read_text()
    cfg = load_config(mira)
    assert cfg['provider']['api_key_env'] == 'DEEPSEEK_API_KEY'
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'explicit-override')
    load_env_file(cfg['shared_env_file'])
    assert os.environ['DEEPSEEK_API_KEY'] == 'explicit-override'


def test_duplicate_character_process_lock_but_parallel_characters_work(tmp_path):
    with character_lock(tmp_path, 'rowan'):
        with pytest.raises(ValueError, match='already running'):
            with character_lock(tmp_path, 'rowan'):
                pass
        with character_lock(tmp_path, 'mira'):
            pass
    with character_lock(tmp_path, 'rowan'):
        pass


def test_onboard_checkins_use_explicit_subset_and_keep_testing_manual(tmp_path):
    root = project(tmp_path)
    path, _, _ = create_character(root, 'rowan', 'Rowan', [100, 200], 'token', 'key',
                                 tools=True, initiative_channels=[100])
    cfg = load_config(path)
    assert cfg['initiative'] == {'enabled': True, 'interval_minutes': 30, 'channel_ids': {100}}
    assert cfg['allowed_channel_ids'] == {100, 200}


@pytest.mark.parametrize('tools,channels', [(False, [100]), (True, [300]), (True, [True])])
def test_onboard_rejects_invalid_checkins_before_writing_credentials(tmp_path, tools, channels):
    root = project(tmp_path)
    with pytest.raises(ValueError):
        create_character(root, 'rowan', 'Rowan', [100, 200], 'token', 'key',
                         tools=tools, initiative_channels=channels)
    assert not (root / '.secrets').exists()
