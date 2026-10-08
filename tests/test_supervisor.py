import asyncio
import copy

import pytest
import yaml

from bevvycord.supervisor import load_supervisor, character_environment, run_supervisor


def test_manifest_enabled_paths_and_duplicate_ids(tmp_path):
    cfg = yaml.safe_load(open('config.example.yaml'))
    cfg['character']['id'] = 'faust'
    (tmp_path / 'faust.yaml').write_text(yaml.safe_dump(cfg))
    path = tmp_path / 'supervisor.yaml'
    entries = [{'config': 'faust.yaml'}, {'config': 'missing.yaml', 'enabled': False}]
    path.write_text(yaml.safe_dump({'characters': entries}))
    configs, pause = load_supervisor(path)
    assert [c['character']['id'] for c in configs] == ['faust']
    assert configs[0]['storage_dir'] == str(tmp_path / 'data')
    assert pause == 15
    entries.append({'config': 'faust.yaml'})
    path.write_text(yaml.safe_dump({'characters': entries}))
    with pytest.raises(ValueError, match='Duplicate'):
        load_supervisor(path)


def test_character_credentials_are_scoped(tmp_path, monkeypatch):
    monkeypatch.delenv('TEST_CHARACTER_TOKEN', raising=False)
    a, b = tmp_path / 'a.env', tmp_path / 'b.env'
    a.write_text('TEST_CHARACTER_TOKEN=first\n')
    b.write_text('TEST_CHARACTER_TOKEN=second\n')
    assert character_environment({'env_file': a})['TEST_CHARACTER_TOKEN'] == 'first'
    assert character_environment({'env_file': b})['TEST_CHARACTER_TOKEN'] == 'second'
    monkeypatch.setenv('TEST_CHARACTER_TOKEN', 'override')
    assert character_environment({'env_file': a})['TEST_CHARACTER_TOKEN'] == 'override'


def test_supervisor_isolates_failure_and_closes_clients(tmp_path, monkeypatch):
    from bevvycord import bot, supervisor
    events = []
    coordinators = []
    class Provider:
        def __init__(self, cfg, environment=None): pass
        async def close(self): events.append('provider-closed')
    class Client:
        def __init__(self, cfg, store, provider, coordinator, environment=None):
            self.name = cfg['character']['id']
            coordinators.append(coordinator)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): events.append(self.name + '-closed')
        async def start(self, token):
            if self.name == 'bad':
                raise RuntimeError('private exception')
            await asyncio.sleep(0)
            events.append('good-started')
    monkeypatch.setattr(supervisor, 'Provider', Provider)
    monkeypatch.setattr(bot, 'CharacterBot', Client)
    monkeypatch.setattr(supervisor, 'character_environment', lambda cfg: {'DISCORD_BOT_TOKEN': 'fake'})
    configs = [{'character': {'id': name}, 'storage_dir': str(tmp_path),
                'memory': {}, 'provider': {}} for name in ('bad', 'good')]
    asyncio.run(run_supervisor(configs, 15))
    assert coordinators[0] is coordinators[1]
    assert 'good-started' in events
    assert 'bad-closed' in events and 'good-closed' in events
    assert events.count('provider-closed') == 2


def test_supervisor_search_plugin_uses_each_characters_env_file(tmp_path, monkeypatch):
    from bevvycord.bot import CharacterBot
    from bevvycord.storage import Store
    from test_agent_flow import config
    monkeypatch.delenv('BEVVYCORD_SEARCH_URL', raising=False)
    async def scenario():
        bots, stores = [], []
        try:
            for name in ('faust', 'scooter'):
                env_file = tmp_path / (name + '.env')
                env_file.write_text(f'BEVVYCORD_SEARCH_URL=https://{name}.example.invalid\n')
                cfg = config()
                cfg['character']['id'] = name
                cfg['env_file'] = str(env_file)
                cfg['tools']['plugins'] = ['bevvycord.plugins.search']
                store = Store(tmp_path / 'data', name)
                stores.append(store)
                environment = character_environment(cfg)
                bot = CharacterBot(cfg, store, None, environment=environment)
                bots.append(bot)
                assert bot.runtime.registry.environment['BEVVYCORD_SEARCH_URL'] == f'https://{name}.example.invalid'
                assert 'web_search' in bot.runtime.registry.tools
        finally:
            for bot in bots: await bot.close()
            for store in stores: store.db.close()
    asyncio.run(scenario())


def test_supervisor_safe_startup_error_does_not_expose_arbitrary_exceptions(tmp_path, monkeypatch, caplog):
    from bevvycord import supervisor
    configs = [{'character': {'id': 'faust'}, 'storage_dir': str(tmp_path)}]
    def fail(cfg):
        raise ValueError('Search plugin requires BEVVYCORD_SEARCH_URL for your SearXNG instance')
    monkeypatch.setattr(supervisor, 'character_environment', fail)
    asyncio.run(run_supervisor(configs, 15))
    assert 'Search plugin requires BEVVYCORD_SEARCH_URL' in caplog.text
    caplog.clear()
    def private(cfg): raise ValueError('secret credential data')
    monkeypatch.setattr(supervisor, 'character_environment', private)
    asyncio.run(run_supervisor(configs, 15))
    assert 'secret credential data' not in caplog.text
