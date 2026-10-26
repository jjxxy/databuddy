"""Explicit folder access for the local computer profile.

Inputs are immutable snapshots read through root-relative no-follow descriptors.
Original files are never overwritten. Live project code and immutable exports
are visible in separate managed subdirectories of the selected folder.
"""
from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import zipfile

if os.name=='nt':
    from .windows_dirfd import os

from .config import DATA, MAX_UPLOAD
from .locks import FileLock
from .store import all_items, get, now, put, seal, uid

CODE_DIR = 'Atlas工作区'
OUTPUT_DIR = 'Atlas成果'
EXTENSIONS = {'.csv', '.xlsx', '.json', '.jsonl', '.pdf', '.docx', '.pptx',
              '.png', '.jpg', '.jpeg', '.tiff', '.webp', '.html', '.htm', '.txt', '.md'}
PRIVATE_NAMES = {'llm.json', 'llm.example.json', 'llm.multi.example.json', 'connector.key',
                 'credentials.json', 'secrets.json', 'id_rsa', 'id_ed25519', 'token.json',
                 'auth.json', 'opencode.json', 'package-lock.json'}
MAX_DIRECTORY_ENTRIES = 5000
MAX_EXPORT_BYTES = 256 * 1024 * 1024


def _csv_literal(value):
    if isinstance(value,str) and (value.startswith(('\t','\r','\n')) or value.lstrip(' \t\r\n').startswith(('=','+','-','@'))):
        return "'"+value
    return value


def enabled():
    return os.getenv('ATLAS_LOCAL_MODE')=='1' and bool(os.getenv('ATLAS_LOCAL_WORKSPACE'))


def root():
    value = os.getenv('ATLAS_LOCAL_WORKSPACE', '')
    if not enabled():
        raise ValueError('本实例未启用本地文件夹工作空间')
    path = Path(value)
    if not path.is_absolute() or path == Path(path.anchor) or path.is_symlink():
        raise ValueError('本地工作空间路径无效')
    if os.name=='nt':
        from .platform_files import assert_plain_path
        try:assert_plain_path(path,missing_ok=False)
        except OSError:raise ValueError('本地工作空间不能包含链接或重解析点') from None
    if not path.is_dir():
        raise ValueError('选择的本地文件夹不可用，请重新挂载后启动')
    return path


def status():
    if not enabled():
        return {'enabled': False, 'mode': 'server'}
    folder = root()
    return {'enabled': True, 'mode': 'local_folder',
            'label': os.getenv('ATLAS_LOCAL_LABEL') or folder.name,
            'code_directory': CODE_DIR, 'output_directory': OUTPUT_DIR,
            'imports_are_snapshots': True, 'originals_overwritten': False,
            'network_mode': os.getenv('ATLAS_NETWORK_MODE', 'connected')}


def _parts(relative, *, managed=False, allow_empty=False):
    if not isinstance(relative, str) or len(relative) > 1000 or '\\' in relative or '\x00' in relative:
        raise ValueError('本地文件路径无效')
    p = PurePosixPath(relative)
    if not relative and allow_empty:
        return ()
    if not relative or p.is_absolute() or any(v in ('', '.', '..') for v in relative.split('/')):
        raise ValueError('只能访问已选文件夹内的相对路径')
    parts = p.parts
    if os.name=='nt' and any(':' in v or v.rstrip(' .')!=v or re.fullmatch(r'(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',v) for v in parts):
        raise ValueError('本地文件名不能包含数据流、设备名称或有歧义的结尾')
    if any(v.startswith('.') or any(ord(c) < 32 for c in v) for v in parts):
        raise ValueError('不能访问隐藏文件、私有配置或上级目录')
    if not managed and any((v.casefold() in {CODE_DIR.casefold(),OUTPUT_DIR.casefold()} if os.name=='nt' else v in (CODE_DIR,OUTPUT_DIR)) or v.lower() in PRIVATE_NAMES
                           or v.lower().startswith(('llm.', 'credentials.', 'secrets.'))
                           or Path(v).suffix.lower() in ('.key', '.pem', '.p12', '.pfx', '.env') for v in parts):
        raise ValueError('不能将系统成果、模型配置或凭据作为本地资料读取')
    return parts


@contextlib.contextmanager
def _directory(relative='', *, managed=False, create=False):
    """Keep directory ownership through every component, including renames."""
    parts = _parts(relative, managed=managed, allow_empty=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    descriptors = []
    try:
        current = os.open(root(), flags); descriptors.append(current)
        for part in parts:
            if create:
                try:os.mkdir(part, mode=0o700, dir_fd=current)
                except FileExistsError:pass
            current = os.open(part, flags, dir_fd=current); descriptors.append(current)
        yield current
    except OSError:
        raise ValueError('本地路径不存在、无权限或包含不允许的链接') from None
    finally:
        for descriptor in reversed(descriptors):os.close(descriptor)


def project_folder(pid):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', pid):raise ValueError('项目标识无效')
    get('project', pid)
    with _directory(CODE_DIR + '/' + pid, managed=True, create=True):pass
    return root() / CODE_DIR / pid


def _project_path(pid, name):
    project_folder(pid)
    parts = _parts(name, managed=True)
    if any(part in ('pipelines','project-state.json') for part in parts):raise ValueError('不能修改内部目录')
    if Path(parts[-1]).suffix.lower() not in ('.md','.txt','.csv','.json','.yaml','.yml','.sql','.py'):
        raise ValueError('只支持可版本化的文本文件')
    return CODE_DIR + '/' + pid + '/files', parts


def read_project_file(pid, name):
    base, parts = _project_path(pid, name)
    with _directory(base + ('/' + '/'.join(parts[:-1]) if len(parts)>1 else ''), managed=True) as parent:
        descriptor = os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        with os.fdopen(descriptor,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink>1 or info.st_size>256*1024:
                raise ValueError('项目文本必须是256KB以内的普通文件')
            content=stream.read(256*1024+1)
            after=os.fstat(stream.fileno())
            if (info.st_size,info.st_mtime_ns,info.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                raise ValueError('项目文件读取时发生变化，请重新读取')
    try:return content.decode('utf-8')
    except UnicodeError:raise ValueError('项目文本须为UTF-8编码') from None


def write_project_file(pid, name, content):
    from .locks import assert_owned
    base,parts=_project_path(pid,name)
    raw=content.encode('utf-8')
    if len(raw)>256*1024:raise ValueError('单个文件不能超过256KB')
    relative=base+('/'+'/'.join(parts[:-1]) if len(parts)>1 else '')
    with _directory(relative,managed=True,create=True) as parent:
        try:
            existing=os.stat(parts[-1],dir_fd=parent,follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode) or existing.st_nlink>1:raise ValueError('不能修改链接或特殊文件')
        except FileNotFoundError:pass
        temporary='.atlas-write-'+uid()
        try:
            descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
            with os.fdopen(descriptor,'wb') as output:
                output.write(raw);output.flush();os.fsync(output.fileno())
            assert_owned()
            with _directory(relative,managed=True) as check:
                if (os.fstat(check).st_dev,os.fstat(check).st_ino)!=(os.fstat(parent).st_dev,os.fstat(parent).st_ino):
                    raise ValueError('工作目录发生变化，未保存文件')
            os.replace(temporary,parts[-1],src_dir_fd=parent,dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:os.unlink(temporary,dir_fd=parent)
            except FileNotFoundError:pass
    return {'path':name,'bytes':len(raw)}


def list_project_files(pid):
    project_folder(pid)
    base=CODE_DIR+'/'+pid+'/files'
    with _directory(base,managed=True,create=True):pass
    files=[];visited=0
    def walk(relative):
        nonlocal visited
        visited+=1
        if visited>1000:raise ValueError('项目目录数量超过限制')
        with _directory(base+('/'+relative if relative else ''),managed=True) as descriptor:
            names=os.listdir(descriptor)
            if len(names)>5000:raise ValueError('项目目录条目超过限制')
            for name in names:
                if name.startswith('.'):continue
                path='/'.join(v for v in (relative,name) if v)
                info=os.stat(name,dir_fd=descriptor,follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):raise ValueError('项目工作区不能包含链接')
                if stat.S_ISDIR(info.st_mode):walk(path)
                elif stat.S_ISREG(info.st_mode):
                    _project_path(pid,path)
                    if info.st_nlink>1:raise ValueError('项目工作区不能包含硬链接')
                    files.append({'path':path,'bytes':info.st_size})
                    if len(files)>5000:raise ValueError('项目文件数量超过限制')
    walk('')
    return sorted(files,key=lambda item:item['path'])


def delete_project_file(pid,name):
    from .locks import assert_owned
    base,parts=_project_path(pid,name)
    with _directory(base+('/'+'/'.join(parts[:-1]) if len(parts)>1 else ''),managed=True) as parent:
        info=os.stat(parts[-1],dir_fd=parent,follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink>1:raise ValueError('不能删除链接或特殊文件')
        assert_owned();os.unlink(parts[-1],dir_fd=parent);os.fsync(parent)
    return {'path':name,'deleted':True}


def files(relative='', offset=0, limit=100):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('本地目录分页参数无效')
    _parts(relative, allow_empty=True)
    entries = []
    with _directory(relative) as descriptor:
        names = os.listdir(descriptor)
        if len(names) > MAX_DIRECTORY_ENTRIES:
            raise ValueError('目录条目超过5000个，请选择更具体的资料文件夹')
        for name in names:
            path = '/'.join(p for p in (relative, name) if p)
            try:_parts(path)
            except ValueError:continue
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):continue
            directory = stat.S_ISDIR(info.st_mode)
            if not directory and (not stat.S_ISREG(info.st_mode) or Path(name).suffix.lower() not in EXTENSIONS or info.st_nlink > 1):continue
            entries.append({'path': path, 'name': name, 'kind': 'directory' if directory else 'file',
                            'bytes': None if directory else info.st_size,
                            'modified_ns': info.st_mtime_ns})
    entries.sort(key=lambda v: (v['kind'] != 'directory', v['name'].casefold()))
    end = offset + limit
    return {'path': relative, 'entries': entries[offset:end], 'total': len(entries),
            'next_offset': end if end < len(entries) else None, 'snapshot': True}


def _copy_input(relative):
    parts = _parts(relative)
    suffix = Path(parts[-1]).suffix.lower()
    if suffix not in EXTENSIONS:raise ValueError('不支持的本地资料格式')
    folder = DATA / 'uploads';folder.mkdir(exist_ok=True)
    target = folder / (uid() + suffix)
    descriptor = None
    try:
        with _directory('/'.join(parts[:-1])) as parent:
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), dir_fd=parent)
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode) or initial.st_nlink > 1:
            raise ValueError('本地输入必须是普通文件，不能是链接或设备')
        if not 0 < initial.st_size <= MAX_UPLOAD:raise ValueError('本地文件为空或超过上传体积限制')
        checksum = hashlib.sha256();size = 0
        with os.fdopen(descriptor, 'rb') as source, target.open('xb') as output:
            descriptor = None
            while block := source.read(1024 * 1024):
                size += len(block)
                if size > MAX_UPLOAD:raise ValueError('本地文件超过上传体积限制')
                checksum.update(block);output.write(block)
            final = os.fstat(source.fileno())
            if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns) != (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) or size != initial.st_size:
                raise ValueError('本地文件在读取过程中变化，请保存文件后重新导入')
            output.flush();os.fsync(output.fileno())
        with _directory('/'.join(parts[:-1])) as parent:
            current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns):
                raise ValueError('本地文件在读取过程中被替换，请保存文件后重新导入')
        if suffix in ('.xlsx', '.docx', '.pptx'):
            with zipfile.ZipFile(target) as archive:
                if len(archive.infolist()) > 10000 or sum(item.file_size for item in archive.infolist()) > 200 * 1024 * 1024:
                    raise ValueError('Office文件解压体积或条目数超过限制')
        return {'stored_name': target.name, 'name': parts[-1][:120], 'bytes': size,
                'content_hash': checksum.hexdigest(), 'local_origin': {
                    'path': relative, 'sha256': checksum.hexdigest(), 'bytes': size,
                    'modified_ns': initial.st_mtime_ns, 'imported_at': now(), 'snapshot': True}}
    except zipfile.BadZipFile:
        target.unlink(missing_ok=True);raise ValueError('Office文件格式损坏') from None
    except BaseException:
        target.unlink(missing_ok=True);raise
    finally:
        if descriptor is not None:os.close(descriptor)


def import_files(pid, paths, request_id):
    root();get('project', pid)
    if not isinstance(paths, list) or not 1 <= len(paths) <= 20 or len(set(paths)) != len(paths):
        raise ValueError('每批请选择1至20个不同的本地文件')
    if os.name=='nt' and len({v.casefold() for v in paths if isinstance(v,str)})!=len(paths):
        raise ValueError('每批不能重复选择同一个Windows文件')
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_id):raise ValueError('本地导入标识无效')
    from .conversation import stable
    from .actor_context import current_actor
    actor=current_actor()
    key=stable('li_',pid,actor,request_id)
    with FileLock(str(DATA/('local-import-batch-'+key+'.lock')),timeout=10):
        try:batch=get('local_import',key)
        except KeyError:batch=put('local_import',{'project_id':pid,'paths':paths,'sources':[],
            'owner_id':actor,'failures':[],'next_index':0,'state':'running','created_at':now()},key)
        if batch['project_id']!=pid or batch.get('owner_id')!=actor or batch['paths']!=paths:raise ValueError('同一导入标识不能更换资料列表')
        if batch['state']=='done':return batch['result']
        return _continue_import(pid,paths,request_id,key,batch)


def _continue_import(pid,paths,request_id,key,batch):
    from .conversation import stable
    from .sources import revision
    from .execution import enqueue
    sources=list(batch['sources']);failures=list(batch['failures'])
    for index,relative in enumerate(paths[batch['next_index']:],batch['next_index']):
        try:
            info = _copy_input(relative)
            identity_path=relative.casefold() if os.name=='nt' else relative
            sid = stable('s_', pid, 'local:' + identity_path + ':' + info['content_hash'])
            target = DATA / 'uploads' / info['stored_name']
            with FileLock(str(DATA / ('local-import-' + sid + '.lock')), timeout=5):
                try:
                    source = get('source', sid)
                    if source.get('project_id') != pid or source.get('content_hash') != info['content_hash']:
                        raise ValueError('本地导入快照标识冲突')
                    target.unlink(missing_ok=True)
                except KeyError:
                    source = put('source', {'name': info['name'], 'kind': 'file',
                        'secret_config': seal({'stored_name': info['stored_name']}),
                        'content_hash': info['content_hash'], 'bytes': info['bytes'], 'goal': '',
                        'created_at': now(), 'status': 'queued', 'project_id': pid,
                        'local_origin': info['local_origin'],
                        **({'skip_model_planning':True} if os.getenv('ATLAS_DESKTOP_MODE')=='1' else {})}, sid)
                    source = revision(source)
                if source['status'] != 'ready':enqueue('probe', sid)
            sources.append({key: source.get(key) for key in ('id', 'name', 'kind', 'revision', 'revision_id', 'content_hash', 'bytes', 'local_origin')})
        except (ValueError, OSError) as error:
            failures.append({'path': relative, 'error': str(error) if isinstance(error, ValueError) else '本地文件读取失败，请检查权限和状态'})
        batch.update(sources=sources,failures=failures,next_index=index+1)
        put('local_import',batch,key)
    result={'sources': sources, 'failures': failures, 'partial': bool(sources and failures),
            'request_id': request_id, 'originals_overwritten': False}
    put('local_import',dict(batch,state='done',result=result),key)
    return result


def export_project(pid):
    """Copy only real, verified files into a new visible immutable snapshot."""
    root();get('project', pid)
    from . import workspace, office, business_reports, project_data
    from .jobs import get_job
    export_id = 'export_' + uid()
    relative = OUTPUT_DIR + '/' + pid
    stage_name='.'+export_id+'.partial'
    failures = [];records = [];total = 0
    with FileLock(str(DATA / ('local-export-' + pid + '.lock')), timeout=30), workspace.lock(pid), _directory(relative, managed=True, create=True) as parent_fd:
        os.mkdir(stage_name,mode=0o700,dir_fd=parent_fd)
        stage_fd=os.open(stage_name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|getattr(os,'O_ATLAS_STAGE',0),dir_fd=parent_fd)
        # Linux libraries use /proc/self/fd; native Windows holds every parent
        # without SHARE_DELETE, so this visible staging path cannot be swapped.
        # Publication renames the same open staging handle on that filesystem.
        stage=os.descriptor_path(stage_fd) if os.name=='nt' else Path('/proc/self/fd')/str(stage_fd)
        try:
            def copy(source, name, expected=None):
                nonlocal total
                if isinstance(source,bytes):content=source
                else:
                    source = Path(source)
                    if source.is_symlink() or not source.is_file():raise ValueError('产物不是普通文件')
                    content = source.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                if expected and digest != expected:raise ValueError('产物与已验证摘要不一致')
                total += len(content)
                if total > MAX_EXPORT_BYTES:raise ValueError('本次成果超过256MB，请按项目或版本分别导出')
                destination = stage / name;destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                records.append({'path': name, 'bytes': len(content), 'sha256': digest})
            for item in workspace.list_files(pid):
                # Internal/path traversal names cannot become export destinations.
                if any(part.startswith('.') for part in Path(item['path']).parts):continue
                copy(workspace.read_file(pid, item['path']).encode('utf-8'), 'code/' + item['path'])
            for item in all_items('office_artifact'):
                if item.get('project_id') != pid:continue
                try:
                    file, metadata = office.artifact(pid, item['id'])
                    copy(file, 'office/' + item['id'] + '-' + Path(metadata['name']).name, metadata['sha256'])
                except (ValueError, OSError, KeyError):failures.append({'kind': 'office', 'id': item['id'], 'error': '该产物未通过完整性核验，未导出'})
            for item in business_reports.list_reports(pid):
                for format in ('markdown', 'manifest', *item.get('office_exports', {}).keys()):
                    if format not in ('markdown', 'manifest', 'docx', 'pptx'):continue
                    try:
                        if format in ('docx','pptx'):
                            # These caches are already committed and immutable.
                            # report_file(format) may generate a cache and acquire
                            # a second project lock; export must only verify and
                            # copy existing bytes while holding this lock.
                            manifest_file=business_reports.report_file(pid,item['id'],'manifest')
                            file=manifest_file.parent/('report.'+format)
                            expected=item['office_exports'][format]
                        else:
                            file=business_reports.report_file(pid,item['id'],format)
                            expected=None
                        copy(file, 'reports/' + item['id'] + '/' + file.name, expected)
                    except (ValueError, OSError, KeyError):failures.append({'kind': 'report', 'id': item['id'], 'format': format, 'error': '该报告未通过完整性核验，未导出'})
            # Export current adopted/candidate references, not an arbitrary
            # first page of historical jobs. Older versions remain in Atlas.
            workflow_runs = set()
            for item in all_items('data_workflow'):
                if item.get('project_id') == pid:
                    workflow_runs.update(v for v in (item.get('published_run'), item.get('candidate_run')) if v)
            for run_id in sorted(workflow_runs):
                try:
                    run = get_job(run_id)
                    for format in ('handoff', 'xlsx'):
                        file = project_data.result_download(pid, run['id'], format)
                        copy(file, 'data/' + run['id'] + '/' + file.name)
                except (ValueError, OSError, KeyError):failures.append({'kind': 'data', 'id': run_id, 'error': '完整结果未通过核验，未导出'})
            for pipeline in all_items('pipeline'):
                if pipeline.get('project_id') != pid:continue
                for run_id in sorted({v for v in (pipeline.get('published_run'), pipeline.get('candidate_run')) if v}):
                    try:
                        from .releases import manifest_for, verify_semantics
                        import duckdb
                        run = get_job(run_id)
                        if run.get('target') != pipeline['id'] or not run.get('validated') or run.get('artifact_removed') or run.get('status') not in ('validated', 'succeeded'):
                            raise ValueError('数据版本不可用')
                        folder, manifest = manifest_for(run)
                        parquet = folder / 'business_objects.parquet'
                        if hashlib.sha256(parquet.read_bytes()).hexdigest() != manifest['parquet_hash']:
                            raise ValueError('完整结果摘要不一致')
                        verify_semantics(pipeline, run, manifest, folder)
                        csv_file = stage / ('pipeline-' + run_id + '.csv')
                        with duckdb.connect() as db, csv_file.open('w',encoding='utf-8-sig',newline='') as output:
                            cursor = db.read_parquet(str(parquet)).execute()
                            labels = {field['name']:field['label'] for field in manifest['fields']}
                            keep = [i for i,field in enumerate(cursor.description) if not field[0].startswith('_dlt')]
                            writer = csv.writer(output)
                            writer.writerow([_csv_literal(labels.get(cursor.description[i][0],cursor.description[i][0])) for i in keep])
                            while rows := cursor.fetchmany(1000):
                                writer.writerows([[_csv_literal(row[i]) for i in keep] for row in rows])
                            output.flush();os.fsync(output.fileno())
                        copy(csv_file, 'applications/' + pipeline['id'] + '/' + run_id + '/result.csv')
                        csv_file.unlink()
                        copy(folder / 'output.json', 'applications/' + pipeline['id'] + '/' + run_id + '/manifest.json')
                    except (ValueError, OSError, KeyError):failures.append({'kind':'application','id':pipeline['id'],'run_id':run_id,'error':'应用完整结果未通过核验，未导出'})
            manifest = {'id': export_id, 'project_id': pid, 'created_at': now(), 'files': records,
                        'failures': failures, 'complete': not failures, 'originals_overwritten': False,
                        'code_is_live_at': CODE_DIR + '/' + pid + '/files',
                        'data_scope':'current adopted and candidate versions; historical versions remain in Atlas',
                        'csv_text_policy':'Spreadsheet formula-like strings and labels are apostrophe-prefixed; numeric values retain their exact decimal representation'}
            (stage / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            from .platform_files import fsync_file
            for file in stage.rglob('*'):
                if file.is_file():
                    fsync_file(file)
            for directory,_,_ in os.walk(stage,topdown=False):
                if Path(directory)==stage:
                    os.fsync(stage_fd);continue
                directory_fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                try:os.fsync(directory_fd)
                finally:os.close(directory_fd)
            with _directory(relative, managed=True) as checked_parent:
                if os.fstat(checked_parent).st_ino != os.fstat(parent_fd).st_ino or os.fstat(checked_parent).st_dev != os.fstat(parent_fd).st_dev:
                    raise ValueError('成果目录在写入过程中变化，未发布')
            # Descriptor-relative publication cannot follow a replaced ancestor.
            try:os.stat(export_id,dir_fd=parent_fd,follow_symlinks=False)
            except FileNotFoundError:pass
            else:raise ValueError('同名成果目录已存在，未覆盖')
            if os.name=='nt':os.publish_directory(stage_fd,export_id,parent_fd)
            else:os.rename(stage_name, export_id, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
            put('local_export', dict(manifest, relative_path=relative + '/' + export_id), export_id)
        except BaseException:
            if os.name=='nt':
                os.close(stage_fd);stage_fd=None
                try:os.remove_tree(stage_name,dir_fd=parent_fd)
                except OSError:pass
            else:shutil.rmtree(stage_name,dir_fd=parent_fd,ignore_errors=True)
            raise
        finally:
            if stage_fd is not None:os.close(stage_fd)
    return {'id': export_id, 'relative_path': relative + '/' + export_id,
            'files': len(records), 'bytes': total, 'complete': not failures, 'failures': failures}


def verify_export(pid, export_id):
    """Verify a saved receipt against every file currently visible on disk.

    The metadata database is authoritative; a user-editable manifest cannot
    turn an incomplete or modified export into a completed task receipt.
    """
    root();get('project',pid)
    if not isinstance(export_id,str) or not re.fullmatch(r'export_[a-f0-9]{24}',export_id):
        raise ValueError('本地成果标识无效')
    record=get('local_export',export_id)
    relative=OUTPUT_DIR+'/'+pid+'/'+export_id
    if record.get('project_id')!=pid or record.get('relative_path')!=relative:
        raise ValueError('本地成果不属于当前项目')
    if record.get('complete') is not True or record.get('failures'):
        raise ValueError('本地成果存在未完成项，不能作为完成凭证')
    expected={key:value for key,value in record.items() if key!='relative_path'}
    records=record.get('files')
    if not isinstance(records,list) or len(records)>10000:
        raise ValueError('本地成果文件清单无效或超过核验范围')
    expected_paths=set();total=0;observed={}

    def signature(info):
        return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)

    def read_file(name, size=None, checksum=None, manifest=False):
        parts=_parts(name,managed=True)
        parent_relative=relative+('/'+'/'.join(parts[:-1]) if len(parts)>1 else '')
        with _directory(parent_relative,managed=True) as parent:
            descriptor=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            with os.fdopen(descriptor,'rb') as source:
                before=os.fstat(source.fileno())
                bound=8*1024*1024 if manifest else MAX_EXPORT_BYTES
                if not stat.S_ISREG(before.st_mode) or before.st_nlink>1 or before.st_size>bound:
                    raise ValueError('本地成果包含链接、特殊文件或超限文件')
                if size is not None and before.st_size!=size:raise ValueError('本地成果大小与完成凭证不一致')
                digest=hashlib.sha256();count=0;content=[]
                while block:=source.read(1024*1024):
                    count+=len(block)
                    if count>bound:raise ValueError('本地成果在核验时超出体积限制')
                    digest.update(block)
                    if manifest:content.append(block)
                after=os.fstat(source.fileno())
                if signature(before)!=signature(after) or count!=before.st_size:
                    raise ValueError('本地成果在核验过程中变化')
                if checksum is not None and digest.hexdigest()!=checksum:
                    raise ValueError('本地成果摘要与完成凭证不一致')
            current=os.stat(parts[-1],dir_fd=parent,follow_symlinks=False)
            if signature(current)!=signature(before):raise ValueError('本地成果在核验过程中被替换')
        observed[name]=signature(before)
        return b''.join(content),digest.hexdigest()

    with _directory(relative,managed=True) as export_fd:
        export_signature=(os.fstat(export_fd).st_dev,os.fstat(export_fd).st_ino)
        raw,manifest_hash=read_file('manifest.json',manifest=True)
        try:actual=json.loads(raw)
        except (ValueError,UnicodeError):raise ValueError('本地成果清单损坏') from None
        if actual!=expected:raise ValueError('本地成果清单与已保存完成凭证不一致')
        for item in records:
            if not isinstance(item,dict):raise ValueError('本地成果文件清单无效')
            name=item.get('path');size=item.get('bytes');checksum=item.get('sha256')
            _parts(name,managed=True)
            if name=='manifest.json' or name in expected_paths or type(size) is not int or size<0 or not isinstance(checksum,str) or not re.fullmatch(r'[a-f0-9]{64}',checksum):
                raise ValueError('本地成果文件清单无效')
            expected_paths.add(name);total+=size
            if total>MAX_EXPORT_BYTES:raise ValueError('本地成果总大小超过核验范围')
            read_file(name,size,checksum)
        actual_paths=set();directories=0
        def walk(subpath=''):
            nonlocal directories
            directories+=1
            if directories>10000:raise ValueError('本地成果目录数量超过核验范围')
            with _directory(relative+('/'+subpath if subpath else ''),managed=True) as directory:
                names=os.listdir(directory)
                if len(names)>10000:raise ValueError('本地成果目录条目超过核验范围')
                for name in names:
                    name='/'.join(v for v in (subpath,name) if v)
                    parts=_parts(name,managed=True)
                    info=os.stat(parts[-1],dir_fd=directory,follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):walk(name)
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink==1:
                        actual_paths.add(name)
                        if name not in observed or signature(info)!=observed[name]:raise ValueError('本地成果含有新增或被替换的文件')
                    else:raise ValueError('本地成果包含链接或特殊文件')
        walk()
        if actual_paths!=expected_paths|{'manifest.json'}:raise ValueError('本地成果文件清单不完整')
        with _directory(relative,managed=True) as current:
            if (os.fstat(current).st_dev,os.fstat(current).st_ino)!=export_signature:
                raise ValueError('本地成果目录在核验过程中被替换')
    return {'id':export_id,'project_id':pid,'relative_path':relative,'files':len(records),
            'bytes':total,'complete':True,'manifest_sha256':manifest_hash}
