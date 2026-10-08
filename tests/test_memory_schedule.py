from bevvycord.context import Message, Window
from bevvycord.storage import Store

SETTINGS = {'quiet_minutes': 30, 'cooldown_hours': 4, 'max_wait_hours': 24}


def setup(tmp_path):
    now = [100000.0]
    store = Store(tmp_path, 'rowan', clock=lambda: now[0])
    return store, now


def participate(store, number=1):
    message = Message(number, 9, 'Rowan', '2026-10-07T00:00:00+00:00', 'An experience', True)
    store.complete(100, Window([message], last_response_id=number), '2026-10-07T00:00:00+00:00')


def save(store):
    _, checkpoint, notes = store.archive(100)
    store.write_memory(100, '- A memory', checkpoint, notes)


def test_first_quiet_period_then_cooldown_then_no_idle_rewrites(tmp_path):
    store, now = setup(tmp_path)
    assert store.due_memory_channels(SETTINGS) == []
    participate(store)
    now[0] += 1799
    assert store.due_memory_channels(SETTINGS) == []
    now[0] += 1
    assert store.due_memory_channels(SETTINGS) == ['100']
    save(store)
    now[0] += 60
    participate(store, 2)
    now[0] += 1800
    assert store.due_memory_channels(SETTINGS) == []
    now[0] += 4 * 3600 - 1860
    assert store.due_memory_channels(SETTINGS) == ['100']
    save(store)
    now[0] += 7 * 86400
    assert store.due_memory_channels(SETTINGS) == []


def test_activity_postpones_quiet_but_max_wait_forces_update(tmp_path):
    store, now = setup(tmp_path)
    participate(store)
    start = now[0]
    for i in range(1, 96):
        now[0] = start + i * 900
        participate(store, i + 1)
        assert store.due_memory_channels(SETTINGS) == []
    now[0] = start + 86400
    store.note_invocation(100)
    assert store.due_memory_channels(SETTINGS) == ['100']


def test_pending_cooldown_and_failure_backoff_survive_restart(tmp_path):
    store, now = setup(tmp_path)
    participate(store)
    now[0] += 1800
    save(store)
    participate(store, 2)
    store.db.close()
    store = Store(tmp_path, 'rowan', clock=lambda: now[0])
    now[0] += 4 * 3600
    assert store.due_memory_channels(SETTINGS) == ['100']
    store.memory_failed(100, 30)
    store.db.close()
    store = Store(tmp_path, 'rowan', clock=lambda: now[0])
    assert store.due_memory_channels(SETTINGS) == []
    now[0] += 1800
    assert store.due_memory_channels(SETTINGS) == ['100']


def test_correction_is_pending_and_keeps_last_success_cooldown(tmp_path):
    store, now = setup(tmp_path)
    participate(store)
    save(store)
    store.delete(100, 1)
    now[0] += 1800
    assert store.due_memory_channels(SETTINGS) == []
    now[0] += 4 * 3600 - 1800
    assert store.due_memory_channels(SETTINGS) == ['100']


def test_legacy_archive_gains_schedule_without_changing_history(tmp_path):
    store, now = setup(tmp_path)
    participate(store)
    original = store.previous(100).dumps()
    store.db.execute('DROP TABLE memory_schedule')
    store.db.commit()
    store.db.close()
    store = Store(tmp_path, 'rowan', clock=lambda: now[0])
    assert store.previous(100).dumps() == original
    assert store.db.execute('SELECT pending_since FROM memory_schedule WHERE channel="100"').fetchone()[0] is not None
