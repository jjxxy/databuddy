"""Read-only comparison of two recorded workspace commits.

A diff describes versioned files, not financial correctness or source approval.
"""
import difflib
import subprocess
from .process_control import executable
from . import workspace

MAX_FILES = 500
MAX_LIST_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 128 * 1024
MAX_LINES = 4000
MAX_DIFF_BYTES = 192 * 1024


GIT_TIMEOUT_SECONDS = 30


def _git(repo, *args, limit=MAX_LIST_BYTES):
    """Bound subprocess output as it arrives; never write unbounded temp files."""
    import queue
    import threading
    import time
    try:
        process = subprocess.Popen([executable('git'), '--literal-pathspecs', '-C', str(repo), *args],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    except OSError:
        raise ValueError('版本内容暂无法读取，请核对版本记录') from None
    pending = queue.Queue(maxsize=2)
    stopped = threading.Event()
    eof = object()
    read_error = object()

    def offer(item):
        while not stopped.is_set():
            try:
                pending.put(item, timeout=.1)
                return
            except queue.Full:
                pass

    def reader():
        try:
            while not stopped.is_set():
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    offer(eof)
                    return
                offer(chunk)
        except (OSError, ValueError):
            offer(read_error)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    result = bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('版本内容读取超时，请稍后重试')
            try:
                part = pending.get(timeout=remaining)
            except queue.Empty:
                raise ValueError('版本内容读取超时，请稍后重试') from None
            if part is eof:
                break
            if part is read_error:
                raise ValueError('版本内容读取失败，请稍后重试')
            if len(result) + len(part) > limit:
                raise ValueError('版本差异过大，无法完整列出；请缩小比较范围')
            result.extend(part)
        try:
            status = process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise ValueError('版本内容读取超时，请稍后重试') from None
        if status != 0:
            raise ValueError('版本内容暂无法读取，请核对版本记录')
        return bytes(result)
    finally:
        stopped.set()
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            raise ValueError('版本比较进程未能确认停止，请检查服务状态') from None
        finally:
            thread.join(timeout=1)
            process.stdout.close()


def _blob(repo, commit, path):
    ref = commit + ':' + path
    try:
        size = int(_git(repo, 'cat-file', '-s', ref, limit=100))
    except ValueError:
        return None
    if size > MAX_TEXT_BYTES:
        return False
    data = _git(repo, 'cat-file', 'blob', ref, limit=MAX_TEXT_BYTES)
    if b'\0' in data:
        return False
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return False
    if len(text.splitlines()) > MAX_LINES:
        return False
    return text


def compare(pid, base, target, path=None):
    # Caller holds the project restore barrier throughout this read.
    older = workspace.resolve(pid, base)['commit']
    newer = workspace.resolve(pid, target)['commit']
    repo = workspace.folder(pid)
    raw = _git(repo, 'diff', '--name-status', '-z', '--no-ext-diff', '--no-renames', older, newer, '--')
    parts = raw.split(b'\0')
    if parts and parts[-1] == b'':
        parts.pop()
    if len(parts) % 2 or len(parts)//2 > MAX_FILES:
        raise ValueError('版本差异文件过多，无法完整列出；请缩小比较范围')
    files = []
    try:
        for i in range(0, len(parts), 2):
            status = parts[i].decode('ascii')
            name = parts[i+1].decode('utf-8')
            if status not in ('A', 'M', 'D', 'T') or not name:
                raise ValueError('版本差异包含不支持的文件状态')
            files.append({'path': name, 'status': status})
    except UnicodeDecodeError:
        raise ValueError('版本差异包含无法安全展示的文件名') from None
    result = {'base': older, 'target': newer, 'files': files,
              'notice': '这里只比较项目版本中的文件变更，不能据此认定业务口径或财务数据正确。'}
    if path is None:
        return result
    selected = next((f for f in files if f['path'] == path), None)
    if not selected:
        raise ValueError('所选文件不属于这两个版本的差异')
    before = _blob(repo, older, path) if selected['status'] != 'A' else None
    after = _blob(repo, newer, path) if selected['status'] != 'D' else None
    if before is False or after is False:
        result['file'] = {'path': path, 'status': selected['status'], 'kind': 'unavailable',
                          'note': '二进制文件、过大文件或复杂文本暂不支持逐行比较；请分别下载对应版本核对。'}
        return result
    if selected['status'] != 'A' and before is None or selected['status'] != 'D' and after is None:
        raise ValueError('版本文件缺失，无法安全比较')
    lines = difflib.unified_diff((before or '').splitlines(keepends=True), (after or '').splitlines(keepends=True),
                                 fromfile='旧版本', tofile='新版本', n=3)
    result_lines = []
    total = 0
    truncated = False
    for line in lines:
        # difflib preserves a missing final newline; without an explicit marker
        # the removed and added lines can run together and mislead the reader.
        if not line.endswith(('\n', '\r')):
            line += '\n\\ No newline at end of file\n'
        n = len(line.encode('utf-8'))
        if total + n > MAX_DIFF_BYTES:
            truncated = True
            break
        total += n
        result_lines.append(line)
    result['file'] = {'path': path, 'status': selected['status'], 'kind': 'text',
                      'diff': ''.join(result_lines), 'truncated': truncated,
                      'note': '差异过长，仅显示部分；请下载两个版本查看完整文件。' if truncated else '按原文本逐行比较。'}
    return result
