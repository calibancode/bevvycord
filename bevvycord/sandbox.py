"""Networkless Linux workspace execution. Never falls back to a host shell."""
import asyncio
import os
from pathlib import Path
import shutil
import signal


class Sandbox:
    def __init__(self, settings):
        self.settings = settings

    async def execute(self, workspace, command, timeout=None, log_path=None):
        bwrap, prlimit = shutil.which('bwrap'), shutil.which('prlimit')
        if not bwrap or not prlimit:
            raise RuntimeError('Sandbox requires bubblewrap and util-linux prlimit')
        workspace = Path(workspace).resolve()
        for name in ('.tmp', '.shm'):
            scratch = workspace / name
            if scratch.is_symlink():
                raise ValueError('Sandbox scratch directories cannot be symlinks')
            scratch.mkdir(exist_ok=True, mode=0o700)
        timeout = min(timeout or self.settings['exec_seconds'], self.settings['exec_seconds'])
        argv = [bwrap, '--unshare-all', '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
                '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
                '--symlink', 'usr/lib', '/lib',
                '--symlink', 'usr/lib64' if Path('/usr/lib64').exists() else 'usr/lib', '/lib64',
                '--proc', '/proc', '--dev', '/dev',
                '--bind', str(workspace / '.tmp'), '/tmp',
                '--bind', str(workspace / '.shm'), '/dev/shm',
                '--bind', str(workspace), '/workspace', '--chdir', '/workspace',
                '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
                '--setenv', 'HOME', '/workspace', '--setenv', 'LANG', 'C.UTF-8',
                '--setenv', 'TMPDIR', '/workspace/.tmp',
                '--', prlimit, f'--cpu={self.settings["exec_seconds"]}',
                f'--as={self.settings["memory_mb"] * 1024 * 1024}',
                f'--fsize={self.settings["file_bytes"]}', '--nofile=128', '--nproc=64',
                '--', '/bin/bash', '--noprofile', '--norc', '-c', command]
        log_file = open(log_path, 'xb') if log_path else None
        try:
            process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                           stderr=asyncio.subprocess.STDOUT,
                                                           env={'PATH': '/usr/bin:/bin'}, start_new_session=True)
        except BaseException:
            if log_file:
                log_file.close()
            raise
        captured, total = bytearray(), 0
        limit = self.settings['output_chars'] * 4
        logged = 0

        async def drain():
            nonlocal total, logged
            while data := await process.stdout.read(8192):
                total += len(data)
                captured.extend(data[:max(0, limit - len(captured))])
                if log_file:
                    piece = data[:max(0, self.settings['file_bytes'] - logged)]
                    log_file.write(piece)
                    logged += len(piece)

        def check_storage():
            size, count = 0, 0
            for root, dirs, files in os.walk(workspace, followlinks=False):
                count += len(dirs) + len(files)
                for name in files:
                    try:
                        size += (Path(root) / name).lstat().st_size
                    except FileNotFoundError:
                        pass
                if size > self.settings['workspace_bytes'] or count > self.settings['workspace_files']:
                    raise RuntimeError('Workspace storage limit exceeded')

        async def monitor():
            while process.returncode is None:
                await asyncio.to_thread(check_storage)
                await asyncio.sleep(0.1)

        reader, watcher = asyncio.create_task(drain()), asyncio.create_task(monitor())
        waiter = asyncio.create_task(process.wait())
        status = 'completed'
        try:
            done, _ = await asyncio.wait([waiter, watcher], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                watcher.result()
            if not done:
                status = 'timeout'
            elif waiter in done:
                await waiter
        finally:
            # Terminate the namespace even if the shell exited leaving children
            # holding pipes open. Cancellation follows the same cleanup path.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            watcher.cancel()
            waiter.cancel()
            await asyncio.gather(watcher, waiter, return_exceptions=True)
            try:
                await asyncio.wait_for(reader, 2)
            except asyncio.TimeoutError:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            finally:
                if log_file:
                    log_file.close()
        await asyncio.to_thread(check_storage)
        output = captured.decode('utf-8', errors='replace')[:self.settings['output_chars']]
        return {'status': status, 'exit_code': process.returncode, 'output': output,
                'log_truncated': bool(log_file and total > logged),
                'truncated': total > len(captured) or len(captured.decode('utf-8', errors='replace')) > self.settings['output_chars']}
