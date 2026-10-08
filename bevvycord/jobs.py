"""Per-turn workspace, bounded files and channel-scoped attachment handles."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from .sandbox import Sandbox
from .filesystem import filesystem_call


@dataclass
class Attachment:
    id: str
    filename: str
    size: int
    download: object  # async callback(destination: Path, max_bytes: int)


@dataclass
class Artifact:
    path: Path
    filename: str
    size: int
    sha256: str


class Job:
    def __init__(self, store, channel, actor, trigger_id, window, settings, attachments=(), upload_limit=None, initiative=False):
        self.store, self.channel, self.actor, self.trigger_id = store, channel, actor, trigger_id
        self.id = uuid.uuid4().hex
        self.settings, self.window = settings, window
        self.root = store.root / 'jobs' / str(int(channel)) / self.id
        self.work = self.root / 'work'
        self.frozen = self.root / 'artifacts'
        self.root.mkdir(parents=True, mode=0o700)
        self.work.mkdir(mode=0o700)
        self.frozen.mkdir(mode=0o700)
        self.attachments = {a.id: a for a in attachments}
        self.downloaded, self.artifacts = {}, []
        self.logs = {}
        self.call_id = None
        self.initiative, self.decision = initiative, None
        self.upload_limit = min(upload_limit or settings['file_bytes'], settings['file_bytes'])
        self.sandbox = Sandbox(settings)
        store.create_job(self.id, channel, actor, trigger_id)

    def environment(self):
        inventory = '\n'.join(f'{a.id}: {a.filename!r} ({a.size} bytes)' for a in self.attachments.values()) or '(none)'
        return ('Tools work privately during this turn; only your final reply, returned files and chosen reactions go to Discord. '
                'Tool results are working material. Use /workspace for files; get_attachment uses the listed IDs; '
                'return_file stages a file for your final reply. Shell execution has no network.\n'
                f'Tool budget: {self.settings["max_steps"] - 1} working model requests, '
                f'{self.settings["max_calls"]} tool calls; stage files before your final reply.\n'
                f'Job ID: {self.id}\nAttachments available in this conversation:\n{inventory}'
                + ('\nYou’re catching up on the channel without being summoned. Join in, follow up on '
                   'something you remember, react, or stay quiet as suits you. Use finish to choose.\nCheck-in time: '
                   + datetime.fromtimestamp(self.store.clock(), timezone.utc).isoformat() if self.initiative else
                   '\nSomeone is addressing you. Respond to their latest message.'))

    def finish(self, text=None, reply_to=None, reactions=()):
        ids = {m.id for m in self.window.messages}
        if reply_to is not None and reply_to not in ids:
            raise ValueError('Reply target must be in this conversation')
        seen = set()
        for reaction in reactions:
            key = (reaction['message_id'], reaction['emoji'])
            if key[0] not in ids:
                raise ValueError('Reaction target must be in this conversation')
            if key in seen or not key[1].strip():
                raise ValueError('Reactions must be nonempty and distinct')
            seen.add(key)
        if text is not None and not text.strip() and not self.artifacts:
            raise ValueError('A text reply cannot be blank; omit text for silence')
        self.decision = {'text': text or '', 'reply_to': reply_to, 'reactions': list(reactions),
                         'send_files': text is not None}
        return {'status': 'finished', 'reply': bool(text or text is not None and self.artifacts), 'reactions': len(reactions)}

    def path(self, value, create_parent=False):
        # Paths are relative to the workspace (or its virtual /workspace mount).
        if value.startswith('/workspace/'):
            value = value[len('/workspace/'):]
        relative = Path(value)
        if (not value or len(value) > 2048 or len(relative.parts) > 32 or '\x00' in value
                or relative.is_absolute() or '..' in relative.parts or relative == Path('.')):
            raise ValueError('Use a relative workspace file path without ..')
        path = self.work
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise ValueError('Symlinks are not accepted by file tools')
        if create_parent:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def open_read(self, value):
        path = self.path(value)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        handle = os.fdopen(fd, 'rb')
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > self.settings['file_bytes']:
            handle.close()
            raise ValueError('Expected a regular file within the size limit')
        return handle

    def read_file(self, path, offset_bytes=0):
        if path.startswith('log:'):
            if path not in self.logs:
                raise ValueError('Unknown execution log handle')
            handle = self.logs[path].open('rb')
        else:
            handle = self.open_read(path)
        with handle:
            # Byte offset is deliberate: deterministic paging for large files.
            handle.seek(offset_bytes)
            data = handle.read(self.settings['output_chars'] * 4 + 1)
        value = data.decode('utf-8', errors='replace')
        return {'content': value[:self.settings['output_chars']],
                'truncated': len(value) > self.settings['output_chars']}

    def check_storage(self, extra=0):
        size, count = 0, 0
        for root, dirs, files in os.walk(self.work, followlinks=False):
            count += len(dirs) + len(files)
            for name in files:
                size += (Path(root) / name).lstat().st_size
        if size + extra > self.settings['workspace_bytes'] or count > self.settings['workspace_files']:
            raise ValueError('Workspace storage limit exceeded')
        return size

    def write_file(self, path, content):
        data = content.encode('utf-8')
        if len(data) > self.settings['file_bytes']:
            raise ValueError('File exceeds size limit')
        self.check_storage(len(data))
        destination = self.path(path, create_parent=True)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError('Expected a regular file')
            handle.write(data)
        return {'path': str(destination.relative_to(self.work)), 'bytes': len(data)}

    async def execute(self, command, timeout=None):
        await filesystem_call(self.check_storage)
        logs = self.root / 'logs'
        logs.mkdir(exist_ok=True)
        log_id = 'log:' + uuid.uuid4().hex
        path = logs / log_id[4:]
        result = await self.sandbox.execute(self.work, command, timeout, log_path=path)
        self.logs[log_id] = path
        result['log'] = log_id
        return result

    def previous_job_info(self, job_id):
        if not re.fullmatch(r'[0-9a-f]{32}', job_id) or job_id == self.id:
            raise ValueError('Invalid previous job ID')
        row = self.store.db.execute('SELECT channel,actor,state,detail FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or row[:2] != (str(self.channel), str(self.actor)):
            raise ValueError('Job is outside your channel and requester scope')
        if row[2] in ('running', 'delivering'):
            raise ValueError('Previous job is still running')
        receipts = self.store.db.execute('SELECT name,result FROM tool_receipts WHERE job=? ORDER BY rowid', (job_id,)).fetchall()
        return {'state': row[2], 'detail': row[3],
                'receipts': [{'tool': name, 'result': json_result(result)} for name, result in receipts]}

    def copy_job_files(self, job_id):
        source = self.store.root / 'jobs' / str(int(self.channel)) / job_id / 'work'
        if not source.is_dir() or source.is_symlink():
            raise ValueError('Previous workspace has expired or is unavailable')
        target = self.path('prior/' + job_id, create_parent=True)
        if not target.exists():
            files, size, count = [], 0, 0
            for root, dirs, names in os.walk(source, followlinks=False):
                count += len(dirs) + len(names)
                if count > self.settings['workspace_files']:
                    raise ValueError('Previous workspace exceeds entry limit')
                for name in dirs + names:
                    path = Path(root) / name
                    info = path.lstat()
                    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                        raise ValueError('Previous workspace contains unsafe file types')
                    if stat.S_ISREG(info.st_mode):
                        if info.st_size > self.settings['file_bytes']:
                            raise ValueError('Previous file exceeds size limit')
                        size += info.st_size
                        files.append(path)
                    if len(files) > self.settings['workspace_files'] or size > self.settings['workspace_bytes']:
                        raise ValueError('Previous workspace exceeds limits')
            self.check_storage(size)
            target.mkdir()
            for path in files:
                destination = target / path.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination, follow_symlinks=False)
        return str(target.relative_to(self.work))

    def open_job(self, job_id):
        info = self.previous_job_info(job_id)
        return {'path': self.copy_job_files(job_id), **info}

    async def aopen_job(self, job_id):
        # SQLite stays on its owning thread; only filesystem work is offloaded.
        info = self.previous_job_info(job_id)
        path = await filesystem_call(self.copy_job_files, job_id)
        return {'path': path, **info}

    async def get_attachment(self, attachment_id):
        if attachment_id not in self.attachments:
            raise ValueError('Attachment is outside this conversation')
        if attachment_id in self.downloaded:
            return self.downloaded[attachment_id]
        attachment = self.attachments[attachment_id]
        if attachment.size > self.settings['file_bytes']:
            raise ValueError('Attachment exceeds the configured size limit')
        await filesystem_call(self.check_storage, attachment.size)
        # Do not use user-controlled filenames or model paths for downloads.
        basename = re.sub(r'[^A-Za-z0-9._-]', '_', attachment.filename)[:100] or 'attachment'
        path = self.path(f'inputs/{uuid.uuid4().hex[:8]}-{basename}', create_parent=True)
        try:
            await attachment.download(path, self.settings['file_bytes'])
            with self.open_read(str(path.relative_to(self.work))):
                pass
            await filesystem_call(self.check_storage)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        result = {'path': str(path.relative_to(self.work)), 'bytes': path.stat().st_size}
        self.downloaded[attachment_id] = result
        return result

    def return_file(self, path, filename=None):
        if len(self.artifacts) >= self.settings['max_artifacts']:
            raise ValueError('Too many returned files')
        filename = filename or Path(path).name
        if not filename or filename in ('.', '..') or '/' in filename or '\\' in filename or any(ord(c) < 32 for c in filename):
            raise ValueError('Invalid delivery filename')
        target = self.frozen / str(len(self.artifacts))
        with self.open_read(path) as source, target.open('xb') as dest:
            data = source.read(self.upload_limit + 1)
            if len(data) > self.upload_limit:
                target.unlink()
                raise ValueError('File exceeds the Discord upload limit')
            dest.write(data)
        artifact = Artifact(target, filename, len(data), hashlib.sha256(data).hexdigest())
        self.artifacts.append(artifact)
        return {'artifact': len(self.artifacts) - 1, 'filename': filename, 'bytes': len(data), 'sha256': artifact.sha256}

    def memory_note(self, kind, note, message_ids=None):
        ids = set(message_ids) if message_ids else {self.trigger_id}
        if not ids.issubset({m.id for m in self.window.messages}):
            raise ValueError('Memory sources must be messages in this conversation')
        self.store.add_note(kind, self.id, self.call_id, self.channel, self.actor, note, ids)
        return {'status': f'{kind} request queued; your memory file changes at the next memory update', 'note': note}


def clean_jobs(store, settings):
    """Expire terminal workspaces, retain small database receipts for inspection."""
    cutoff = store.clock() - settings['retention_days'] * 86400
    rows = store.db.execute("SELECT id,channel FROM jobs WHERE updated<? AND state NOT IN ('running','delivering')", (cutoff,)).fetchall()
    for job_id, channel in rows:
        if re.fullmatch(r'[0-9a-f]{32}', job_id) and channel.isdigit():
            path = store.root / 'jobs' / channel / job_id
            if path.exists() and not path.is_symlink():
                shutil.rmtree(path)


def json_result(result):
    import json
    if result is None:
        return {'status': 'uncertain; do not repeat without inspecting'}
    value = json.loads(result)
    return value if len(result) <= 1000 else {'preview': result[:1000]}
