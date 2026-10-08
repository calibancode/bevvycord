"""Real opt-in Bubblewrap checks; no host execution or external services."""
import asyncio
import os
from pathlib import Path
import time
import threading

import pytest

from bevvycord.config import tool_settings
from bevvycord.sandbox import Sandbox

pytestmark = pytest.mark.skipif(os.environ.get('BEVVYCORD_SANDBOX_TESTS') != '1', reason='Set BEVVYCORD_SANDBOX_TESTS=1 on a Linux host with Bubblewrap namespaces')


def test_storage_monitor_walks_off_the_gateway_thread(tmp_path, monkeypatch):
    main_thread = threading.get_ident()
    threads = []
    original = os.walk
    def walk(*args, **kwargs):
        threads.append(threading.get_ident())
        yield from original(*args, **kwargs)
    monkeypatch.setattr('bevvycord.sandbox.os.walk', walk)
    result = asyncio.run(Sandbox(tool_settings({})).execute(tmp_path, 'printf ok'))
    assert result['exit_code'] == 0 and result['output'] == 'ok'
    assert threads and main_thread not in threads


def test_real_transformation_environment_network_and_bounds(tmp_path, monkeypatch):
    settings = tool_settings({'tools': {'output_chars': 500, 'exec_seconds': 5}})
    sandbox = Sandbox(settings)
    monkeypatch.setenv('BEVVYCORD_SECRET_SENTINEL', 'must-never-enter')
    (tmp_path / 'input.ppm').write_bytes(b'P6\n2 1\n255\n' + bytes([255, 0, 0, 0, 255, 0]))
    async def scenario():
        result = await sandbox.execute(tmp_path, 'magick input.ppm -resize 8x4! output.png && magick identify output.png')
        assert result['exit_code'] == 0, result
        assert '8x4' in result['output']
        assert (tmp_path / 'output.png').read_bytes().startswith(b'\x89PNG')
        result = await sandbox.execute(tmp_path, "python3 - <<'CODE'\nimport os, socket\nassert 'BEVVYCORD_SECRET_SENTINEL' not in os.environ\nassert not os.path.exists('/home')\nassert not os.path.exists('/workspace/../.secrets')\ntry:\n socket.create_connection(('1.1.1.1',443),1)\nexcept OSError:\n print('network blocked')\nelse:\n raise AssertionError('network allowed')\nprint('isolated')\nCODE")
        assert result['exit_code'] == 0, result
        assert 'isolated' in result['output']
        result = await sandbox.execute(tmp_path, "python3 -c 'print(\"x\" * 100000)'", log_path=tmp_path.parent / 'sandbox-log.txt')
        assert len(result['output']) == 500 and result['truncated']
        assert (tmp_path.parent / 'sandbox-log.txt').stat().st_size > 500
    asyncio.run(scenario())


def test_timeout_and_cancellation_kill_descendants(tmp_path):
    sandbox = Sandbox(tool_settings({'tools': {'exec_seconds': 2}}))
    async def scenario():
        # The child keeps writing after its shell starts waiting. Writes must stop
        # once timeout/cancellation returns, including all descendants.
        command = "setsid python3 -c 'import time; from pathlib import Path; p=Path(\"ticks\"); [(p.write_text(str(i)),time.sleep(.05)) for i in range(500)]' & wait"
        result = await sandbox.execute(tmp_path, command, timeout=1)
        assert result['status'] == 'timeout'
        old = (tmp_path / 'ticks').read_text()
        await asyncio.sleep(.2)
        assert (tmp_path / 'ticks').read_text() == old
        task = asyncio.create_task(sandbox.execute(tmp_path, command))
        await asyncio.sleep(.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        old = (tmp_path / 'ticks').read_text()
        await asyncio.sleep(.2)
        assert (tmp_path / 'ticks').read_text() == old
    asyncio.run(scenario())


def test_real_agent_attachment_transform_delivery_and_memory(tmp_path, monkeypatch):
    from test_agent_flow import run_file_memory_flow
    run_file_memory_flow(tmp_path, monkeypatch, real_exec=True)


def test_scratch_storage_file_limits_and_symlink_mount_rejection(tmp_path):
    settings = tool_settings({'tools': {'workspace_bytes': 8192, 'file_bytes': 4096, 'exec_seconds': 3}})
    sandbox = Sandbox(settings)
    async def scenario():
        result = await sandbox.execute(tmp_path, "python3 -c 'from pathlib import Path; Path(\"big\").write_bytes(b\"x\" * 10000)'")
        assert result['exit_code'] != 0
        assert (tmp_path / 'big').stat().st_size <= 4096
        (tmp_path / 'big').unlink()
        with pytest.raises(RuntimeError, match='storage limit'):
            await sandbox.execute(tmp_path, "python3 -c 'from pathlib import Path; [Path(\"/tmp/\"+str(i)).write_bytes(b\"x\"*2000) for i in range(10)]'")
    asyncio.run(scenario())
    import shutil
    shutil.rmtree(tmp_path / '.tmp')
    (tmp_path / '.tmp').symlink_to('/etc')
    with pytest.raises(ValueError, match='symlinks'):
        asyncio.run(sandbox.execute(tmp_path, 'echo must-not-run'))
