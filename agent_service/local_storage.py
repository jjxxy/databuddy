"""Standard-library-only local mount initialization before application imports."""
import json
import os
from pathlib import Path
import stat


def prepare_storage(uid, gid):
    value=os.getenv('ATLAS_LOCAL_WORKSPACE','')
    path=Path(value)
    if not value or not path.is_absolute() or path==Path(path.anchor):raise ValueError('本地工作文件夹无效')
    expected=path/'.atlas'
    if Path(os.getenv('DATA_DIR',str(expected)))!=expected:raise ValueError('本地模式元数据必须保存在已选文件夹的.atlas内')
    if os.name=='nt':
        return _prepare_windows(path,expected)
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|getattr(os,'O_CLOEXEC',0)
    descriptors=[]
    def directory(parent,name):
        created=False
        try:os.mkdir(name,mode=0o700,dir_fd=parent);created=True
        except FileExistsError:pass
        descriptor=os.open(name,flags,dir_fd=parent);descriptors.append(descriptor)
        if created and os.geteuid()==0:os.fchown(descriptor,uid,gid)
        info=os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=uid:
            raise ValueError('本地Atlas目录属于其他系统用户，请由文件夹所有者修复权限；不会自动改动原文件权限')
        return descriptor,created
    try:
        # Walk the absolute path instead of resolving away a symlink.
        current=os.open(path.anchor,flags);descriptors.append(current)
        for component in path.parts[1:]:
            current=os.open(component,flags,dir_fd=current);descriptors.append(current)
        metadata,created=directory(current,'.atlas')
        marker='local-workspace.identity'
        if created:
            fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=metadata)
            with os.fdopen(fd,'w') as output:
                json.dump({'format':'atlas-local-v1'},output);output.flush();os.fsync(output.fileno())
                if os.geteuid()==0:os.fchown(output.fileno(),uid,gid)
        else:
            fd=os.open(marker,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=metadata)
            with os.fdopen(fd,'rb') as source:
                info=os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size>4096 or json.loads(source.read(4097))!={'format':'atlas-local-v1'}:
                    raise ValueError('已有.atlas目录不是有效的本地工作空间，未修改其内容')
        directory(metadata,'home')
        directory(current,'Atlas工作区');directory(current,'Atlas成果')
        os.fsync(metadata);os.fsync(current)
        return expected
    except (OSError,UnicodeError,json.JSONDecodeError):
        raise ValueError('本地挂载或.atlas包含链接、无权限或不是Atlas工作空间，未修改原始文件') from None
    finally:
        for descriptor in reversed(descriptors):os.close(descriptor)


def _prepare_windows(path,expected):
    from .windows_dirfd import os as windows, owner_sid
    from .platform_files import private_directory, _user_sid
    descriptors=[]
    current_sid=_user_sid()
    def directory(parent,name):
        created=False
        try:windows.mkdir(name,dir_fd=parent);created=True
        except FileExistsError:pass
        descriptor=windows.open(name,windows.O_DIRECTORY|windows.O_NOFOLLOW|windows.O_RDONLY,dir_fd=parent)
        descriptors.append(descriptor)
        if owner_sid(descriptor)!=current_sid:
            raise ValueError('本地Atlas目录属于其他系统用户，未修改其权限或内容')
        if created:private_directory(descriptor.path)
        return descriptor,created
    try:
        current=windows.open(path,windows.O_DIRECTORY|windows.O_NOFOLLOW|windows.O_RDONLY)
        descriptors.append(current)
        metadata,created=directory(current,'.atlas')
        marker='local-workspace.identity'
        if created:
            descriptor=windows.open(marker,windows.O_WRONLY|windows.O_CREAT|windows.O_EXCL|windows.O_NOFOLLOW,dir_fd=metadata)
            with os.fdopen(descriptor,'w',encoding='utf-8') as output:
                json.dump({'format':'atlas-local-v1'},output);output.flush();os.fsync(output.fileno())
        else:
            descriptor=windows.open(marker,windows.O_RDONLY|windows.O_NOFOLLOW,dir_fd=metadata)
            with os.fdopen(descriptor,'rb') as source:
                info=os.fstat(source.fileno())
                if info.st_size>4096 or json.loads(source.read(4097))!={'format':'atlas-local-v1'}:
                    raise ValueError('已有.atlas目录不是有效的本地工作空间，未修改其内容')
            # Restrict only the marker-validated, current-user-owned metadata.
            # The selected business folder and original files keep their ACLs.
            private_directory(metadata.path)
        directory(metadata,'home')
        directory(current,'Atlas工作区');directory(current,'Atlas成果')
        return expected
    except (OSError,UnicodeError,json.JSONDecodeError):
        raise ValueError('本地工作文件夹或.atlas包含链接、无权限或不是Atlas工作空间，未修改原始文件') from None
    finally:
        for descriptor in reversed(descriptors):windows.close(descriptor)
