"""Bounded stdlib-only reader for the operator's optional preset manifest."""
import json,os,stat
from .platform_files import open_regular_read

MAX_PRESET_BYTES=8*1024*1024

def load_presets_file(path):
    descriptor=None
    try:
        descriptor=open_regular_read(path)
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_PRESET_BYTES:raise ValueError('预置配置须为不超过8MB的普通JSON文件')
        with os.fdopen(descriptor,'rb') as stream:
            descriptor=None;raw=stream.read(MAX_PRESET_BYTES+1)
        if len(raw)>MAX_PRESET_BYTES:raise ValueError('预置配置超过大小限制')
        def unique(pairs):
            value={}
            for key,item in pairs:
                if key in value:raise ValueError('预置配置不能有重复字段')
                value[key]=item
            return value
        value=json.loads(raw,object_pairs_hook=unique)
        if not isinstance(value,dict) or set(value)-{'skills','mcp'}:raise ValueError('预置配置字段无效')
        if not isinstance(value.get('skills',[]),list) or not isinstance(value.get('mcp',[]),list) or len(value.get('skills',[]))+len(value.get('mcp',[]))>60:raise ValueError('预置项目过多或结构无效')
        return value
    except (OSError,UnicodeError,ValueError,TypeError):raise ValueError('管理员预置配置读取失败，请检查文件、JSON结构和大小') from None
    finally:
        if descriptor is not None:os.close(descriptor)
