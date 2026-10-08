from pathlib import Path

import pytest
import yaml

from bevvycord.config import load_config


@pytest.mark.parametrize('section,key,value', [
    (None, 'allow_dms', 'false'),
    ('memory', 'enabled', 'false'),
    ('context', 'max_fetch_messages', True),
    (None, 'allowed_channel_ids', '123456'),
    ('memory', 'max_archive_chars', -1),
    ('memory', 'quiet_minutes', 0),
    ('memory', 'cooldown_hours', '4'),
    ('memory', 'max_wait_hours', float('inf')),
    ('memory', 'retry_minutes', True),
    ('memory', 'daily_hour_utc', 8),
    ('initiative', 'enabled', 'false'),
    ('initiative', 'interval_minutes', 0),
    ('initiative', 'interval_minutes', True),
    ('initiative', 'channel_ids', [999]),
    ('initiative', 'channel_ids', [True]),
    ('initiative', 'enabled', True),
])
def test_config_rejects_ambiguous_permissions_and_limits(tmp_path, section, key, value):
    cfg = yaml.safe_load(Path('config.example.yaml').read_text())
    target = cfg[section] if section else cfg
    target[key] = value
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError):
        load_config(path)


def test_quoted_ids_are_consistent_and_default_token_limit_supports_thinking(tmp_path):
    cfg = yaml.safe_load(Path('config.example.yaml').read_text())
    cfg['allowed_channel_ids'] = ['100', 200]
    cfg['tools']['enabled'] = True
    cfg['initiative'].update(enabled=True, channel_ids=['100'])
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(cfg))
    loaded = load_config(path)
    assert loaded['allowed_channel_ids'] == {100, 200}
    assert loaded['initiative']['channel_ids'] == {100}
    assert loaded['provider']['parameters']['max_tokens'] == 65536
    assert loaded['memory']['parameters']['max_tokens'] == 65536
