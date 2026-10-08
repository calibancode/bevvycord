"""Character-owned durable files, copied through the existing workspace boundary."""
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import stat
import uuid

from .filesystem import filesystem_call


log = logging.getLogger(__name__)


class Library:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.root = (Path(settings['storage_dir']) / store.root.name
                     if settings.get('storage_dir') else store.root / 'library')
        if self.root.resolve().is_relative_to((store.root / 'jobs').resolve()):
            raise ValueError('Library storage cannot be inside temporary jobs')
        self.lock = asyncio.Lock()

    def scope(self, job, scope):
        if scope == 'personal':
            return 'personal'
        if scope == 'channel':
            return str(job.channel)
        raise ValueError('Library scope must be channel or personal')

    @staticmethod
    def name(name):
        path = Path(name)
        if (not name or len(name) > 160 or path.is_absolute()
                or any(part in ('', '.', '..') for part in name.split('/'))
                or any(ord(c) < 32 for c in name) or '\\' in name):
            raise ValueError('Use a relative library filename of at most 160 characters without . or .. components')
        return name

    def directory(self):
        if self.root.is_symlink():
            raise ValueError('Library directory cannot be a symlink')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.root

    def blob_path(self, blob):
        if len(blob) != 32 or any(c not in '0123456789abcdef' for c in blob):
            raise ValueError('Invalid library file record')
        return self.directory() / blob

    def row(self, job, name, scope):
        name, scope = self.name(name), self.scope(job, scope)
        row = self.store.db.execute(
            'SELECT scope,name,blob,bytes,modified,job,channel,actor,trigger_id,source_message_ids '
            'FROM library_files WHERE scope=? AND name=?', (scope, name)).fetchone()
        if row is None:
            raise ValueError('No saved file with that name in this scope')
        return row

    @staticmethod
    def metadata(row, provenance=False):
        result = {'name': row[1], 'scope': 'personal' if row[0] == 'personal' else 'channel',
                  'bytes': row[3], 'modified': datetime.fromtimestamp(row[4], timezone.utc).isoformat()}
        if provenance:
            sources = json.loads(row[9])
            result['origin'] = {'job_id': row[5], 'channel_id': row[6], 'initiator_id': row[7],
                                'trigger_id': row[8], 'source_message_ids': sources[:20],
                                'source_count': len(sources)}
        return result

    def list(self, job, prefix='', scope='all', limit=20, offset=0):
        if len(prefix) > 160 or not 1 <= limit <= 20 or offset < 0:
            raise ValueError('Invalid library listing bounds')
        scopes = ('personal', str(job.channel)) if scope == 'all' else (self.scope(job, scope),)
        placeholders = ','.join('?' for _ in scopes)
        # Literal prefix comparison: SQL wildcard characters in filenames are ordinary text.
        where = f'scope IN ({placeholders}) AND substr(name,1,?)=?'
        params = (*scopes, len(prefix), prefix)
        total = self.store.db.execute(f'SELECT COUNT(*) FROM library_files WHERE {where}', params).fetchone()[0]
        rows = self.store.db.execute(
            f'SELECT scope,name,blob,bytes,modified FROM library_files WHERE {where} '
            'ORDER BY modified DESC,name,scope LIMIT ? OFFSET ?', (*params, limit, offset)).fetchall()
        return {'files': [self.metadata(row) for row in rows], 'total': total,
                'next_offset': offset + len(rows) if offset + len(rows) < total else None}

    async def settled(self, operation):
        # Metadata remains on SQLite's owning event loop. Cancellation waits for
        # both the bounded copy and its metadata commit, never leaving a live writer.
        async def run():
            async with self.lock:
                return await operation()
        task = asyncio.create_task(run())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def remove_orphans(self):
        # A crash before metadata commit can leave an unreferenced immutable blob.
        # Recover only our blob namespace, under the character's mutation lock.
        referenced = {row[0] for row in self.store.db.execute('SELECT blob FROM library_files')}
        def cleanup():
            for entry in self.directory().iterdir():
                name = entry.name
                if (len(name) == 32 and all(c in '0123456789abcdef' for c in name)
                        and name not in referenced):
                    entry.unlink(missing_ok=True)
        await filesystem_call(cleanup)

    async def save(self, job, path, name=None, scope='channel', overwrite=False):
        name = self.name(name if name is not None else Path(path).name)
        scope = self.scope(job, scope)
        async def operation():
            await self.remove_orphans()
            old = self.store.db.execute('SELECT blob,bytes FROM library_files WHERE scope=? AND name=?',
                                        (scope, name)).fetchone()
            if old and not overwrite:
                raise ValueError('Saved file already exists; use overwrite=true to replace it')
            count, used = self.store.db.execute('SELECT COUNT(*),COALESCE(SUM(bytes),0) FROM library_files').fetchone()
            if not old and count >= self.settings['max_files']:
                raise ValueError('Library file count limit reached; delete an unneeded file first')
            cap = min(job.settings['file_bytes'], self.settings['file_bytes'],
                      self.settings['max_bytes'] - used + (old[1] if old else 0))
            if cap < 0:
                raise ValueError('Library storage limit reached; delete an unneeded file first')
            blob = uuid.uuid4().hex
            target = self.blob_path(blob)
            def copy():
                with job.open_read(path) as source:
                    data = source.read(cap + 1)
                    if len(data) > cap:
                        raise ValueError('File exceeds library operation or storage limit')
                    with target.open('xb') as dest:
                        dest.write(data)
                        dest.flush()
                        os.fsync(dest.fileno())
                directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                return len(data)
            try:
                size = await filesystem_call(copy)
                now = self.store.clock()
                sources = json.dumps(sorted({job.trigger_id, *(m.id for m in job.window.messages)}))
                with self.store.db:
                    self.store.db.execute('''INSERT INTO library_files VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(scope,name) DO UPDATE SET blob=excluded.blob,bytes=excluded.bytes,
                        modified=excluded.modified,job=excluded.job,channel=excluded.channel,
                        actor=excluded.actor,trigger_id=excluded.trigger_id,source_message_ids=excluded.source_message_ids''',
                        (scope, name, blob, size, now, job.id, str(job.channel), str(job.actor), job.trigger_id, sources))
            except BaseException:
                await filesystem_call(target.unlink, missing_ok=True)
                raise
            if old:
                try:
                    await filesystem_call(self.blob_path(old[0]).unlink, missing_ok=True)
                except OSError:
                    # The new save is committed. Retry leftover cleanup before the next save.
                    log.warning('Library replacement saved; old content cleanup deferred.')
            return self.metadata(self.row(job, name, 'personal' if scope == 'personal' else 'channel'), True)
        return await self.settled(operation)

    async def get(self, job, name, path, scope='channel'):
        async def operation():
            row = self.row(job, name, scope)
            source = self.blob_path(row[2])
            def copy():
                try:
                    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except OSError:
                    raise ValueError('Saved file content is unavailable or unsafe') from None
                with os.fdopen(fd, 'rb') as handle:
                    info = os.fstat(handle.fileno())
                    cap = min(job.settings['file_bytes'], self.settings['file_bytes'])
                    if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
                        raise ValueError('Saved file is not a regular file within operation limits')
                    data = handle.read(cap + 1)
                    if len(data) > cap or len(data) != row[3]:
                        raise ValueError('Saved file size differs from its record or exceeds operation limits')
                target = job.path(path)
                missing_dirs = 0
                parent = target.parent
                while parent != job.work:
                    missing_dirs += int(not parent.exists())
                    parent = parent.parent
                job.check_storage(len(data), extra_files=1 + missing_dirs)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    raise ValueError('Workspace destination already exists; choose another path') from None
                try:
                    with os.fdopen(fd, 'wb') as dest:
                        dest.write(data)
                except BaseException:
                    target.unlink(missing_ok=True)
                    raise
            await filesystem_call(copy)
            return {'path': path, **self.metadata(row, True)}
        return await self.settled(operation)

    async def delete(self, job, name, scope='channel'):
        async def operation():
            row = self.row(job, name, scope)
            # Remove content first; a failed unlink retains the metadata for retry.
            await filesystem_call(self.blob_path(row[2]).unlink, missing_ok=True)
            with self.store.db:
                self.store.db.execute('DELETE FROM library_files WHERE scope=? AND name=?', row[:2])
            return {'deleted': name, 'scope': scope}
        return await self.settled(operation)
