"""Bounded, versioned wire format shared by the controller and runner."""
import base64
import hashlib
import io
import json
import tarfile
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_FILE = 25 * 1024 * 1024
MAX_WORKSPACE = 64 * 1024 * 1024
MAX_FILES = 1000
MAX_ARCHIVE = MAX_WORKSPACE + 4 * 1024 * 1024
MAX_LOG = 64000
TERMINAL = frozenset({'succeeded', 'failed', 'timed_out', 'cancelled', 'unknown'})


def digest(value):
    return hashlib.sha256(value).hexdigest()


def file_path(value):
    path = PurePosixPath(value)
    if (not value or len(value) > 240 or path.is_absolute() or '\\' in value
            or any(part in ('', '.', '..') or part.startswith('.atlas') for part in value.split('/'))
            or len(value.split('/')) > 16 or any(ord(char) < 32 for char in value)):
        raise ValueError('Only ordinary relative workspace paths are allowed')
    return path.as_posix()


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class InputFile(Strict):
    path: str
    content_base64: str = Field(max_length=(MAX_FILE + 2) // 3 * 4)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def validate_bytes(self):
        file_path(self.path)
        if digest(self.bytes()) != self.sha256:
            raise ValueError('Input file digest mismatch')
        return self

    def bytes(self):
        try:
            value = base64.b64decode(self.content_base64, validate=True)
        except ValueError:
            raise ValueError('Invalid base64 input') from None
        if len(value) > MAX_FILE:
            raise ValueError('Input file exceeds size limit')
        return value


class Command(Strict):
    protocol: int = Field(default=1, ge=1, le=1)
    execution_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    workspace_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    command: str = Field(min_length=1, max_length=32000)
    timeout_seconds: int = Field(default=120, ge=1, le=900)
    files: list[InputFile] = Field(default_factory=list, max_length=40)

    @model_validator(mode='after')
    def bounded(self):
        if '\x00' in self.command or not self.command.strip():
            raise ValueError('Invalid command')
        paths = [entry.path for entry in self.files]
        if len(set(paths)) != len(paths) or sum(len(entry.bytes()) for entry in self.files) > MAX_WORKSPACE:
            raise ValueError('Duplicate paths or input budget exceeded')
        return self

    def sha256(self):
        return digest(json.dumps(self.model_dump(), sort_keys=True, separators=(',', ':')).encode())


def pack(files):
    """Create an ordinary-file archive; never preserve ownership or links."""
    result = io.BytesIO()
    directories = {str(parent) for name in files for parent in PurePosixPath(file_path(name)).parents if str(parent) != '.'}
    if directories & set(files) or len(directories) + len(files) > MAX_FILES * 4:
        raise ValueError('Conflicting paths or too many workspace entries')
    with tarfile.open(fileobj=result, mode='w') as archive:
        for name in sorted(directories, key=lambda path: (path.count('/'), path)):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o700
            info.uid = info.gid = 10001
            archive.addfile(info)
        for name, value in sorted(files.items()):
            info = tarfile.TarInfo(file_path(name))
            info.size = len(value)
            info.mode = 0o600
            info.uid = info.gid = 10001
            archive.addfile(info, io.BytesIO(value))
    return result.getvalue()


def unpack(raw, *, docker_root=None):
    """Read bounded regular files in memory. Never extract guest archives on host."""
    result = {}
    size = count = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
        for info in archive:
            count += 1
            if count > MAX_FILES * 4:
                raise ValueError('Too many workspace archive entries')
            name = info.name
            if docker_root:
                if name in (docker_root, docker_root + '/') and info.isdir():
                    continue
                if not name.startswith(docker_root + '/'):
                    raise ValueError('Unexpected Docker archive root')
                name = name[len(docker_root) + 1:]
            name = file_path(name.rstrip('/') if info.isdir() else name)
            if info.isdir():
                continue
            if not info.isreg() or name in result:
                raise ValueError('Links, special files and duplicate files cannot be checkpointed')
            size += info.size
            if info.size < 0 or info.size > MAX_FILE or size > MAX_WORKSPACE or len(result) >= MAX_FILES:
                raise ValueError('Workspace snapshot exceeds size limit')
            result[name] = archive.extractfile(info).read(info.size)
    return result
