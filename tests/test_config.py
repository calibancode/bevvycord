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
