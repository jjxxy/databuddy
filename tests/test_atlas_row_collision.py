"""The synthetic row ordinal must never replace a source value."""
from agent_service import project_data as data
from agent_service.execution import process_once
from agent_service.jobs import get_job
from test_project_data import project, registered, source


def _doc(sid, *, mapped_name):
    cols=[{'name':mapped_name,'data_type':'text','meaning':'source identifier'},
          {'name':'value','data_type':'text','meaning':'source value'}]
    if mapped_name != 'nexus_row':
        cols.insert(0, {'name':'nexus_row','data_type':'text','meaning':'snapshot row ordinal'})
    names=[c['name'] for c in cols]
    return {'version':1,'plan':{'version':1,'name':'Row identity integrity',
        'inputs':{'input':{'meaning':'source rows','grain':['nexus_row'],'columns':cols}},
        'stages':[{'id':'copy','kind':'select','description':'Preserve rows',
                   'input':'input','columns':names}],'output':'copy'},
        'bindings':{'input':{'source_id':sid,'column_map':{'c_1':mapped_name,'c_2':'value'}}}}


def _run(pid,wid):
    queued=data.enqueue_workflow(pid,wid)
    for _ in range(100):
        current=get_job(queued['id'])
        if current['status'] not in ('queued','running','retrying'):return current
        assert process_once()
    raise AssertionError('workflow did not complete')


def test_mapped_real_nexus_row_is_not_overwritten():
    pid=project();s=source(pid,b'original,value\nABC,10\nDEF,20\n')
    wid=registered(pid,_doc(s['id'],mapped_name='nexus_row'))
    result=_run(pid,wid)
    assert result['status']=='failed'
    assert result['error_code']=='INPUT_SCHEMA_MISMATCH'
    assert '不能覆盖原始数据' in result['workflow_failure']['message']
    assert data.owned(pid,'data_workflow',wid).get('published_run') is None


def test_synthetic_row_ordinal_preserves_duplicate_rows():
    pid=project();s=source(pid,b'original,value\nABC,10\nABC,10\n')
    wid=registered(pid,_doc(s['id'],mapped_name='source_id'))
    result=_run(pid,wid)
    assert result['status']=='validated',result
    rows=data.read_result(pid,result['id'])['rows']
    assert [(row['nexus_row'],row['source_id'],row['value']) for row in rows]==[('1','ABC','10'),('2','ABC','10')]
