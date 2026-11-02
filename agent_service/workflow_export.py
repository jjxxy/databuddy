"""Version-checked, project-scoped exports of saved workflow definitions."""
import io
import json
import re
import zipfile
from fastapi import HTTPException
from . import workspace


def _checked(value):
    if not re.fullmatch(r'[a-f0-9]{64}',value or ''):
        raise HTTPException(400,'流程版本无效')


def _zip(files):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,content in files.items():archive.writestr(name,content)
    return out.getvalue()


def data_bundle(pid,wid,expected_digest):
    from . import project_data as data
    from .data_workflows import digest
    from .workspace_lifecycle import assert_project_live
    _checked(expected_digest);assert_project_live(pid)
    with workspace.lock(pid):
        record=data.owned(pid,'data_workflow',wid)
        if record.get('archived'):raise ValueError('流程不可用')
        document=data.load_document(pid,record['path']).model_dump(mode='json')
        if digest(document)!=expected_digest:raise HTTPException(409,'流程已变化，请重新查看后下载')
    # Only the explicitly registered definition is included; never source data,
    # database contents, job snapshots, environment settings or credentials.
    entry=("#!/usr/bin/env python3\nimport json, os, urllib.request, uuid\n"
           "base=os.environ['NEXUS_URL'].rstrip('/')\n"
           "body=json.dumps({'request_id':uuid.uuid4().hex,'expected_draft_sha256':"+repr(expected_digest)+"}).encode()\n"
           "request=urllib.request.Request(base+"+repr('/api/projects/'+pid+'/data-workflows/'+wid+'/run-checked')+",data=body,headers={'Content-Type':'application/json'},method='POST')\n"
           "request.add_header('Authorization','Bearer '+os.environ['NEXUS_TOKEN'])\n"
           "print(urllib.request.urlopen(request,timeout=30).read().decode())\n")
    readme=('本包包含已保存的数据流程定义及调用服务运行该版本的 Python 入口。\n'
            '这是服务端受控执行器的调用入口，不是可独立完成数据处理的代码；运行需要有权限的 Nexus WorkStudio 服务和原项目资料。\n'
            '本包不包含资料原件、运行结果或登录密钥；金额和业务口径仍需核对。\n')
    return _zip({'README.txt':readme,'workflow.json':json.dumps(document,ensure_ascii=False,sort_keys=True,indent=2),'run.py':entry})


def business_bundle(pid,fid,expected_digest,actor):
    from . import business_flows as flow
    _checked(expected_digest)
    with workspace.lock(pid):
        value=flow._owned(pid,fid,actor)
        if value.get('candidate_digest')!=expected_digest:raise HTTPException(409,'流程已变化，请重新查看后下载')
        commit=value.get('candidate_commit')
        flow._read_commit(pid,fid,commit,expected_digest)
        repo=workspace.folder(pid)
        try:
            files={name:workspace.git_content(repo,commit,'files/'+path) for name,path in [('workflow.json',flow._path(fid)),('runtime-lock.json',flow._lock_path(fid)),('run.py',flow._entry_path(fid))]}
        except Exception:raise ValueError('流程对应版本的文件不可用') from None
        if files['run.py'].decode('utf-8')!=flow._entry(pid,fid,expected_digest):raise ValueError('流程执行入口与选定版本不一致')
        try:json.loads(files['runtime-lock.json'])
        except (ValueError,UnicodeDecodeError):raise ValueError('流程版本锁无效') from None
    files['README.txt']=('本包包含保存的流程定义、运行时版本锁和 Python 调用入口。\n入口调用已授权的 Nexus WorkStudio 服务；并非自包含执行器。\n不包含资料原件、运行结果或登录密钥；财务口径仍需核对。\n')
    return _zip(files)
