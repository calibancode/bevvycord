from dataclasses import asdict
from pathlib import Path
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

from .context import Message, Window, transcript


class Store:
    def __init__(self, root, character, clock=None, retain_deleted=True):
        self.clock = clock or time.time
        # Deleted text is kept only until a memory update has seen it retracted.
        self.retain_deleted = retain_deleted
        if not character or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in character):
            raise ValueError('character.id must contain only letters, digits, underscores or hyphens')
        self.root = Path(root) / character
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / 'history.sqlite3')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS messages (
              channel TEXT NOT NULL, id INTEGER NOT NULL, body TEXT NOT NULL,
              deleted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(channel, id));
            CREATE TABLE IF NOT EXISTS interactions (
              id INTEGER PRIMARY KEY, channel TEXT NOT NULL, completed_at TEXT NOT NULL,
              window TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state (channel TEXT PRIMARY KEY, window TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_runs (channel TEXT PRIMARY KEY, interaction_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_schedule (
              channel TEXT PRIMARY KEY, pending_since REAL, last_activity REAL NOT NULL,
              last_success REAL, retry_after REAL);
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, channel TEXT NOT NULL, actor TEXT NOT NULL,
              trigger_id INTEGER NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
              updated REAL NOT NULL, detail TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS tool_receipts (
              job TEXT NOT NULL, call_id TEXT NOT NULL, name TEXT NOT NULL,
              arguments TEXT NOT NULL, result TEXT, PRIMARY KEY(job,call_id));
            CREATE TABLE IF NOT EXISTS deliveries (
              job TEXT NOT NULL, part INTEGER NOT NULL, state TEXT NOT NULL,
              message_id INTEGER, PRIMARY KEY(job,part));
            CREATE TABLE IF NOT EXISTS memory_notes (
              id INTEGER PRIMARY KEY, channel TEXT NOT NULL, kind TEXT NOT NULL,
              actor TEXT NOT NULL, created REAL NOT NULL, note TEXT NOT NULL,
              message_ids TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0,
              job TEXT, call_id TEXT, UNIQUE(job,call_id));
            CREATE TABLE IF NOT EXISTS memory_forced (channel TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS attention (
              channel TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
              seen INTEGER NOT NULL DEFAULT 0, last_check REAL NOT NULL,
              last_message_id INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS reaction_state (
              channel TEXT NOT NULL, message_id INTEGER NOT NULL, data TEXT NOT NULL,
              changed REAL NOT NULL, PRIMARY KEY(channel,message_id));
            CREATE TABLE IF NOT EXISTS attention_messages (
              channel TEXT NOT NULL, id INTEGER NOT NULL, PRIMARY KEY(channel,id));
        ''')
        self._migrate_columns()
        self._migrate_schedule()

    def _migrate_columns(self):
        with self.db:
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(messages)')}
            if 'changed' not in columns:
                self.db.execute('ALTER TABLE messages ADD COLUMN changed REAL')
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(memory_runs)')}
            if 'through' not in columns:
                self.db.execute('ALTER TABLE memory_runs ADD COLUMN through REAL')
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(deliveries)')}
            for key in ('kind', 'emoji'):
                if key not in columns:
                    default = 'message' if key == 'kind' else ''
                    self.db.execute(f"ALTER TABLE deliveries ADD COLUMN {key} TEXT NOT NULL DEFAULT '{default}'")
            # Former remember requests carried transcript copies; keep only the note.
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_requests'").fetchone():
                self.db.execute("INSERT OR IGNORE INTO memory_notes(channel,kind,actor,created,note,message_ids,job,call_id) "
                                "SELECT channel,'remember',actor,created,note,'[]',job,call_id FROM memory_requests")
                self.db.execute('DROP TABLE memory_requests')

    def _migrate_schedule(self):
        # Existing archives acquire timing state without changing their content.
        with self.db:
            for channel, latest in self.db.execute('SELECT channel,MAX(completed_at) FROM interactions GROUP BY channel').fetchall():
                if self.db.execute('SELECT 1 FROM memory_schedule WHERE channel=?', (channel,)).fetchone():
                    continue
                try:
                    activity = datetime.fromisoformat(latest).timestamp()
                except ValueError:
                    activity = self.clock()
                finished = self.db.execute('SELECT interaction_id FROM memory_runs WHERE channel=?', (channel,)).fetchone()
                maximum = self.db.execute('SELECT MAX(id) FROM interactions WHERE channel=?', (channel,)).fetchone()[0]
                path = self.root / channel / 'MEMORY.md'
                success = path.stat().st_mtime if path.exists() else None
                pending = None if finished and finished[0] >= maximum else activity
                self.db.execute('INSERT INTO memory_schedule VALUES (?,?,?,?,NULL)', (channel, pending, activity, success))

    def _mark_pending(self, channel):
        channel, now = str(channel), self.clock()
        if not self.db.execute('SELECT 1 FROM interactions WHERE channel=? LIMIT 1', (channel,)).fetchone():
            return
        self.db.execute('''INSERT INTO memory_schedule VALUES (?,?,?,NULL,NULL)
            ON CONFLICT(channel) DO UPDATE SET
            pending_since=COALESCE(memory_schedule.pending_since,excluded.pending_since),
            last_activity=excluded.last_activity''', (channel, now, now))

    def note_invocation(self, channel):
        with self.db:
            self.db.execute('UPDATE memory_schedule SET last_activity=? WHERE channel=?', (self.clock(), str(channel)))

    def attention(self, channel):
        return self.db.execute('SELECT revision,seen,last_check,last_message_id FROM attention WHERE channel=?',
                               (str(channel),)).fetchone()

    def baseline_attention(self, channel, message_id=0):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO attention(channel,last_check,last_message_id) VALUES (?,?,?)',
                            (str(channel), self.clock(), message_id))

    def note_activity(self, channel, message_id, human=True, event=False):
        self.baseline_attention(channel)
        row = self.attention(channel)
        with self.db:
            increment = int(human and event)
            if human and not event and message_id > row[3]:
                inserted = self.db.execute('INSERT OR IGNORE INTO attention_messages VALUES (?,?)', (str(channel), message_id))
                increment = inserted.rowcount
            self.db.execute('UPDATE attention SET revision=revision+? WHERE channel=?', (increment, str(channel)))
        return self.attention(channel)[0]

    def scanned_attention(self, channel, message_id):
        with self.db:
            self.db.execute('UPDATE attention SET last_message_id=MAX(last_message_id,?) WHERE channel=?',
                            (message_id, str(channel)))
            self.db.execute('DELETE FROM attention_messages WHERE channel=? AND id<=?', (str(channel), message_id))

    def checked_attention(self, channel, revision):
        with self.db:
            self.db.execute('UPDATE attention SET seen=MAX(seen,?),last_check=? WHERE channel=?',
                            (revision, self.clock(), str(channel)))

    def set_reactions(self, channel, message_id, snapshot):
        if self._deleted(channel, message_id):
            return False
        encoded = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        old = self.db.execute('SELECT data FROM reaction_state WHERE channel=? AND message_id=?',
                              (str(channel), message_id)).fetchone()
        if (old and old[0] == encoded) or (not old and not snapshot['members'] and not snapshot['incomplete']):
            return False
        with self.db:
            self.db.execute('INSERT INTO reaction_state VALUES (?,?,?,?) ON CONFLICT(channel,message_id) '
                            'DO UPDATE SET data=excluded.data,changed=excluded.changed',
                            (str(channel), message_id, encoded, self.clock()))
            self._mark_pending(channel)
        return True

    def reactions(self, channel, ids, changed_after=None):
        lines = []
        for message_id in sorted(ids):
            found = self.db.execute('SELECT data,changed FROM reaction_state WHERE channel=? AND message_id=?',
                                    (str(channel), message_id)).fetchone()
            if not found or changed_after is not None and found[1] <= changed_after:
                continue
            snapshot = json.loads(found[0])
            members = snapshot['members']
            suffix = ']' if members else ': unknown]' if snapshot['incomplete'] else ': none]'
            lines.append(f'[Current reactions on message:{message_id}' + suffix)
            for item in members:
                kind = 'bot' if item['bot'] else 'user'
                lines.append(f'  {json.dumps(item["emoji"], ensure_ascii=False)} ({item["kind"]}) by '
                             f'{json.dumps(item["name"], ensure_ascii=False)} ({kind}:{item["actor"]})')
            if snapshot['incomplete']:
                lines.append('  [Reaction list incomplete; additional reactors may not be shown.]')
        return '\n'.join(lines)

    def due_memory_channels(self, settings):
        now = self.clock()
        quiet = settings.get('quiet_minutes', 30) * 60
        cooldown = settings.get('cooldown_hours', 4) * 3600
        maximum = settings.get('max_wait_hours', 24) * 3600
        due = []
        for channel, pending, activity, success, retry in self.db.execute('SELECT * FROM memory_schedule WHERE pending_since IS NOT NULL'):
            if self.db.execute("SELECT 1 FROM jobs WHERE channel=? AND state IN ('running','delivering')", (channel,)).fetchone():
                continue
            deadline = min(max(activity + quiet, success + cooldown if success is not None else 0), pending + maximum)
            if self.db.execute('SELECT 1 FROM memory_forced WHERE channel=?', (channel,)).fetchone():
                deadline = 0
            if now >= max(deadline, retry or 0):
                due.append(channel)
        return due

    def memory_failed(self, channel, retry_minutes=30):
        with self.db:
            self.db.execute('UPDATE memory_schedule SET retry_after=? WHERE channel=?',
                            (self.clock() + retry_minutes * 60, str(channel)))

    def previous(self, channel):
        row = self.db.execute('SELECT window FROM state WHERE channel=?', (str(channel),)).fetchone()
        if not row:
            return None
        window = Window.loads(row[0])
        result = []
        for message in window.messages:
            current = self.db.execute('SELECT body, deleted FROM messages WHERE channel=? AND id=?', (str(channel), message.id)).fetchone()
            if current and current[1]:
                continue
            result.append(Message(**json.loads(current[0])) if current else message)
        window.messages = result
        if window.gap_before not in {m.id for m in result}:
            # Preserve the boundary if its first message was deleted.
            later = [m.id for m in result if window.gap_before and m.id >= window.gap_before]
            window.gap_before = min(later) if later else None
        return window

    def update(self, channel, message):
        self.update_many(channel, [message])

    def update_many(self, channel, messages):
        # One transaction for a whole history fetch; unchanged rows are not rewritten.
        with self.db:
            for message in messages:
                body = json.dumps(asdict(message), ensure_ascii=False)
                old = self.db.execute('SELECT body,deleted FROM messages WHERE channel=? AND id=?', (str(channel), message.id)).fetchone()
                if old and (old[1] or old[0] == body):
                    continue
                self.db.execute('INSERT INTO messages VALUES (?, ?, ?, 0, NULL) ON CONFLICT(channel,id) DO UPDATE SET body=excluded.body WHERE messages.deleted=0',
                                (str(channel), message.id, body))
                # Account metadata changes are not edits to the conversation.
                # Keep any earlier pending content edit's changed timestamp.
                previous = json.loads(old[0]) if old else None
                if previous and (previous.get('text') != message.text
                                 or previous.get('reply_to') != message.reply_to):
                    self.db.execute('UPDATE messages SET changed=? WHERE channel=? AND id=?', (self.clock(), str(channel), message.id))
                    self._mark_pending(channel)

    def delete(self, channel, message_id):
        with self.db:
            old = self.db.execute('SELECT deleted FROM messages WHERE channel=? AND id=?', (str(channel), message_id)).fetchone()
            # Tombstone even an as-yet unarchived message: a pending generation
            # must not reintroduce it when committing its earlier snapshot.
            self.db.execute('INSERT INTO messages VALUES (?, ?, ?, 1, ?) ON CONFLICT(channel,id) DO UPDATE SET deleted=1,changed=excluded.changed',
                            (str(channel), message_id, '{}', self.clock()))
            self.db.execute('DELETE FROM reaction_state WHERE channel=? AND message_id=?', (str(channel), message_id))
            if not self.retain_deleted:
                self.db.execute("UPDATE messages SET body='{}' WHERE channel=? AND id=?", (str(channel), message_id))
            if old and not old[0]:
                self._mark_pending(channel)

    def complete(self, channel, window, timestamp):
        with self.db:
            for message in window.messages:
                self.db.execute('INSERT INTO messages VALUES (?, ?, ?, 0, NULL) ON CONFLICT(channel,id) DO NOTHING',
                                (str(channel), message.id, json.dumps(asdict(message), ensure_ascii=False)))
            self.db.execute('INSERT INTO interactions(channel,completed_at,window) VALUES (?,?,?)', (str(channel), timestamp, window.dumps()))
            self.db.execute('INSERT INTO state VALUES (?,?) ON CONFLICT(channel) DO UPDATE SET window=excluded.window', (str(channel), window.dumps()))
            self._mark_pending(channel)

    def memory_path(self, channel):
        path = self.root / str(int(channel)) / 'MEMORY.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def memory(self, channel):
        path = self.memory_path(channel)
        return path.read_text() if path.exists() else ''

    def speaker_id(self, channel):
        for row in self.db.execute('SELECT window FROM interactions WHERE channel=? ORDER BY id DESC', (str(channel),)):
            window = Window.loads(row[0])
            for message in window.messages:
                if message.id == window.last_response_id:
                    return message.author_id
        return None

    def pending_channels(self):
        return [row[0] for row in self.db.execute('SELECT channel FROM memory_schedule WHERE pending_since IS NOT NULL')]

    def recover_jobs(self):
        with self.db:
            self.db.execute("UPDATE jobs SET state='interrupted',updated=? WHERE state IN ('running','delivering')", (self.clock(),))

    def create_job(self, job_id, channel, actor, trigger_id):
        with self.db:
            self.db.execute("INSERT INTO jobs(id,channel,actor,trigger_id,state,created,updated) VALUES (?,?,?,?,'running',?,?)",
                            (job_id, str(channel), str(actor), trigger_id, self.clock(), self.clock()))

    def job_state(self, job_id, state, detail=''):
        with self.db:
            self.db.execute('UPDATE jobs SET state=?,updated=?,detail=? WHERE id=?', (state, self.clock(), detail, job_id))

    def delivery(self, job_id, part, message_id=None, *, kind='message', emoji='', state=None):
        with self.db:
            self.db.execute('INSERT INTO deliveries(job,part,state,message_id,kind,emoji) VALUES (?,?,?,?,?,?) '
                            'ON CONFLICT(job,part) DO UPDATE SET state=excluded.state,message_id=excluded.message_id',
                            (job_id, part, state or ('sent' if message_id else 'started'), message_id, kind, emoji))

    def receipt_start(self, job_id, call_id, name, arguments):
        with self.db:
            self.db.execute('INSERT INTO tool_receipts(job,call_id,name,arguments) VALUES (?,?,?,?)', (job_id, call_id, name, arguments))

    def receipt_finish(self, job_id, call_id, result):
        with self.db:
            self.db.execute('UPDATE tool_receipts SET result=? WHERE job=? AND call_id=?', (result, job_id, call_id))

    def add_note(self, kind, job_id, call_id, channel, actor, note, message_ids):
        with self.db:
            self.db.execute('INSERT INTO memory_notes(channel,kind,actor,created,note,message_ids,job,call_id) VALUES (?,?,?,?,?,?,?,?)',
                            (str(channel), kind, str(actor), self.clock(), note, json.dumps(sorted(message_ids)), job_id, call_id))
            self.db.execute('''INSERT INTO memory_schedule VALUES (?,?,?,NULL,NULL)
                ON CONFLICT(channel) DO UPDATE SET pending_since=COALESCE(memory_schedule.pending_since,excluded.pending_since),
                last_activity=excluded.last_activity''', (str(channel), self.clock(), self.clock()))
            # Explicit requests bypass quiet/cooldown once the current job settles,
            # but never bypass an existing failure backoff.
            self.db.execute('INSERT OR IGNORE INTO memory_forced VALUES (?)', (str(channel),))

    def has_pending_notes(self, channel):
        return bool(self.db.execute('SELECT 1 FROM memory_notes WHERE channel=? AND applied=0', (str(channel),)).fetchone())

    def notes(self, channel, pending_only=False):
        """Chronological notes. A remember note whose sources were all deleted is
        withdrawn, like any other deleted content; forget notes always stand."""
        rows = self.db.execute('SELECT id,kind,actor,created,note,message_ids FROM memory_notes WHERE channel=?'
                               + (' AND applied=0' if pending_only else '') + ' ORDER BY id', (str(channel),)).fetchall()
        result = []
        for note_id, kind, actor, created, note, raw in rows:
            ids = json.loads(raw)
            if kind == 'remember' and ids and all(self._deleted(channel, i) for i in ids):
                continue
            result.append((note_id, kind, actor, created, note))
        return result

    def _deleted(self, channel, message_id):
        row = self.db.execute('SELECT deleted FROM messages WHERE channel=? AND id=?', (str(channel), message_id)).fetchone()
        return bool(row and row[0])

    @staticmethod
    def render_notes(notes):
        return '\n'.join(f'[{datetime.fromtimestamp(created, timezone.utc).isoformat()} {kind} requested by user:{actor}] '
                         f'{json.dumps(note, ensure_ascii=False)}' for _, kind, actor, created, note in notes)

    def _archived_ids(self, channel, after=None):
        query = 'SELECT window FROM interactions WHERE channel=?' + (' AND id>?' if after is not None else '')
        rows = self.db.execute(query, (str(channel),) + ((after,) if after is not None else ())).fetchall()
        return {m.id for (raw,) in rows for m in Window.loads(raw).messages}

    def _live(self, channel, ids):
        messages = []
        for message_id in sorted(ids):
            row = self.db.execute('SELECT body,deleted FROM messages WHERE channel=? AND id=?', (str(channel), message_id)).fetchone()
            if row and not row[1]:
                messages.append(Message(**json.loads(row[0])))
        return messages

    def checkpoint(self, channel):
        row = self.db.execute('SELECT MAX(id) FROM interactions WHERE channel=?', (str(channel),)).fetchone()
        return row[0] or 0

    def archive(self, channel):
        """Full-refresh input: current versions of all messages encountered in
        successful runs, plus every standing remember/forget note."""
        body = '[Archive note: These are messages encountered during successful participation. '
        body += 'The archive may omit intervening conversation; timestamps and message IDs identify each message.]\n\n'
        body += transcript(Window(self._live(channel, self._archived_ids(channel))))
        reactions = self.reactions(channel, self._archived_ids(channel))
        if reactions:
            body += '\n\n<reactions>\n' + reactions + '\n</reactions>'
        notes = self.notes(channel)
        if notes:
            body += '\n\nMemory requests, oldest first (later conversation and requests supersede earlier ones):\n'
            body += self.render_notes(notes)
        return body, self.checkpoint(channel), [n[0] for n in notes]

    def changes(self, channel):
        """Incremental input since the last memory update: new conversation,
        edits and deletions of earlier messages, and pending notes."""
        row = self.db.execute('SELECT interaction_id,through FROM memory_runs WHERE channel=?', (str(channel),)).fetchone()
        last, through = (row[0], row[1] or 0) if row else (0, 0)
        sections = []
        new_ids = self._archived_ids(channel, last)
        new = self._live(channel, new_ids)
        if new:
            sections.append('<new_conversation>\n' + transcript(Window(new)) + '\n</new_conversation>')
        older = self._archived_ids(channel) - new_ids
        edited, deleted = [], []
        for message_id in sorted(older):
            found = self.db.execute('SELECT body,deleted FROM messages WHERE channel=? AND id=? AND changed>?',
                                    (str(channel), message_id, through)).fetchone()
            if not found or found[0] == '{}':
                continue
            (deleted if found[1] else edited).append(Message(**json.loads(found[0])))
        if edited:
            sections.append('<edited_messages note="Current versions of earlier messages that were edited.">\n'
                            + transcript(Window(edited)) + '\n</edited_messages>')
        if deleted:
            sections.append('<deleted_messages note="Deleted by their author or a moderator. Remove anything '
                            'your memory holds only because of these.">\n' + transcript(Window(deleted)) + '\n</deleted_messages>')
        reactions = self.reactions(channel, new_ids)
        changed_reactions = self.reactions(channel, older, changed_after=through)
        if reactions or changed_reactions:
            sections.append('<current_reactions>\n' + '\n'.join(filter(None, (reactions, changed_reactions)))
                            + '\n</current_reactions>')
        notes = self.notes(channel, pending_only=True)
        if notes:
            sections.append('<memory_requests>\n' + self.render_notes(notes) + '\n</memory_requests>')
        return '\n\n'.join(sections), self.checkpoint(channel), [n[0] for n in notes]

    def write_memory(self, channel, content, interaction_id, note_ids=(), through=None):
        through = self.clock() if through is None else through
        path = self.memory_path(channel)
        # Retain one previous revision for review/recovery.
        if path.exists():
            self._atomic(path.with_name('MEMORY.previous.md'), path.read_text())
        self._atomic(path, content.rstrip() + '\n')
        self._atomic(path.with_name('memory-provenance.json'), json.dumps({'through_interaction': interaction_id}))
        with self.db:
            self.db.execute('INSERT INTO memory_runs VALUES (?,?,?) ON CONFLICT(channel) DO UPDATE SET interaction_id=excluded.interaction_id,through=excluded.through',
                            (str(channel), interaction_id, through))
            self.db.executemany('UPDATE memory_notes SET applied=1 WHERE id=?', [(i,) for i in note_ids])
            # Memory has now seen these retractions; the text itself goes.
            self.db.execute("UPDATE messages SET body='{}' WHERE channel=? AND deleted=1 AND changed<=?", (str(channel), through))
            self.db.execute('UPDATE memory_schedule SET pending_since=NULL,last_success=?,retry_after=NULL WHERE channel=?', (self.clock(), str(channel)))
            self.db.execute('DELETE FROM memory_forced WHERE channel=?', (str(channel),))

    @staticmethod
    def _atomic(path, content):
        temp = path.with_suffix(path.suffix + '.tmp')
        temp.write_text(content, encoding='utf-8')
        os.replace(temp, path)
