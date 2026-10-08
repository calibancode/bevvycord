import asyncio
import json
import sqlite3

import pytest

from bevvycord.jobs import Job, clean_jobs
from bevvycord.storage import Store
from test_runtime import job


def test_origin_survives_reopening_by_another_requester_and_self(tmp_path):
    original, _ = job(tmp_path)
    original.write_file('input.txt', 'attachment-derived work')
    original.store.job_state(original.id, 'complete')
    sources = sorted({original.trigger_id, *(m.id for m in original.window.messages)})
    for actor, initiative in ((8, False), (9, True)):
        current = Job(original.store, 100, actor, 2, original.window, original.settings, initiative=initiative)
        info = asyncio.run(current.aopen_job(original.id))
        assert info['character_id'] == original.character
        assert info['channel_id'] == '100'
        assert info['requester_id'] == info['initiator_id'] == '7'
        assert info['initiative'] is False
        assert info['trigger_id'] == original.trigger_id
        assert info['source_message_ids'] == sources
        assert current.path(info['path'] + '/input.txt').read_text() == 'attachment-derived work'
    record = original.store.db.execute('SELECT actor,source_message_ids,initiative FROM jobs WHERE id=?', (original.id,)).fetchone()
    assert record == ('7', json.dumps(sources), 0)
    original.store.db.close()


def test_self_initiated_job_can_be_reopened_by_human_turn_but_not_other_channel(tmp_path):
    current, _ = job(tmp_path)
    self_job = Job(current.store, 100, 9, 2, current.window, current.settings, initiative=True)
    self_job.write_file('draft.txt', 'draft')
    with pytest.raises(ValueError, match='still running'):
        current.open_job(self_job.id)
    current.store.job_state(self_job.id, 'complete')
    info = current.open_job(self_job.id)
    assert info['requester_id'] is None
    assert info['initiator_id'] == '9' and info['initiative'] is True
    elsewhere = Job(current.store, 200, 9, 2, current.window, current.settings, initiative=True)
    with pytest.raises(ValueError, match='character and channel'):
        elsewhere.open_job(self_job.id)
    current.store.clock = lambda: 10**12
    clean_jobs(current.store, current.settings)
    with pytest.raises(ValueError, match='expired'):
        current.open_job(self_job.id)
    assert current.store.db.execute('SELECT actor,trigger_id FROM jobs WHERE id=?', (self_job.id,)).fetchone() == ('9', 2)
    current.store.db.close()


def test_old_job_metadata_migrates_without_losing_attribution(tmp_path):
    root = tmp_path / 'scooter'
    root.mkdir()
    db = sqlite3.connect(root / 'history.sqlite3')
    db.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY, channel TEXT, actor TEXT, trigger_id INTEGER, state TEXT, created REAL, updated REAL, detail TEXT)')
    db.execute("INSERT INTO jobs VALUES (?, '100', '7', 123, 'complete', 1, 2, '')", ('a' * 32,))
    db.commit()
    db.close()
    store = Store(tmp_path, 'scooter')
    assert store.db.execute('SELECT actor,trigger_id,source_message_ids,initiative FROM jobs').fetchone() == ('7', 123, '[123]', None)
    store.db.close()
    # Reopening an already migrated database preserves metadata.
    store = Store(tmp_path, 'scooter')
    assert store.db.execute('SELECT source_message_ids FROM jobs').fetchone() == ('[123]',)
    store.db.close()
