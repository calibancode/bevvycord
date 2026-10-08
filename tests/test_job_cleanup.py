import asyncio

from bevvycord.bot import CharacterBot
from bevvycord.jobs import clean_jobs
from test_agent_flow import config
from test_runtime import job


def test_daily_expiration_preserves_recent_and_active_jobs_and_archive(tmp_path):
    current, _ = job(tmp_path)
    assert current.settings['retention_days'] == 1
    now = 200000
    current.store.clock = lambda: now
    current.write_file('generated.txt', 'old generated data')
    current.store.complete(100, current.window, '2026-10-07')
    current.store.memory_path(100).write_text('- Remember this')
    with current.store.db:
        current.store.db.execute("UPDATE jobs SET state='complete',updated=? WHERE id=?", (now - 86401, current.id))
    # Keep all jobs under one store so the same cleanup pass checks every state.
    from bevvycord.jobs import Job
    retained = []
    for state, age in [('complete', 86399), ('running', 90000), ('delivering', 90000),
                       ('failed', 90000), ('cancelled', 90000), ('interrupted', 90000)]:
        other = Job(current.store, 100, 7, 2, current.window, current.settings)
        other.write_file('file.txt', state)
        current.store.job_state(other.id, state)
        with current.store.db:
            current.store.db.execute('UPDATE jobs SET updated=? WHERE id=?', (now - age, other.id))
        retained.append((other, state in ('running', 'delivering') or age < 86400))
    clean_jobs(current.store, current.settings)
    assert not current.root.exists()
    for other, expected in retained:
        assert other.root.exists() == expected
    assert current.store.memory(100) == '- Remember this'
    assert current.store.previous(100)
    assert current.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 7
    archive, _, _ = current.store.archive(100)
    assert 'Remember my preference' in archive
    current.store.db.close()


def test_cleanup_runs_immediately_then_checks_hourly(tmp_path, monkeypatch):
    current, _ = job(tmp_path)
    current.store.job_state(current.id, 'complete')
    current.store.clock = lambda: 10**12
    cfg = config()
    # No registry/network initialization needed to exercise the cleanup loop.
    cfg['tools']['enabled'] = False
    bot = CharacterBot(cfg, current.store, None)
    delays = []
    async def stop(delay):
        delays.append(delay)
        assert not current.root.exists()
        raise asyncio.CancelledError
    monkeypatch.setattr('bevvycord.bot.asyncio.sleep', stop)
    async def scenario():
        try:
            await bot.cleanup_loop()
        except asyncio.CancelledError:
            pass
        finally:
            await bot.close()
            current.store.db.close()
    asyncio.run(scenario())
    assert delays == [3600]
