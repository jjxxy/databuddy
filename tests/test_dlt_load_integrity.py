"""A dlt partial destination failure must never become a successful import."""
import json

import pytest
from dlt.common.pipeline import DestinationHasFailedJobs

from agent_service import runner, rules
from agent_service.errors import TaskError


@pytest.mark.parametrize('result', ['failed_job', 'missing_receipt'])
def test_nonempty_dlt_load_must_have_successful_receipt(tmp_path, monkeypatch, result):
    run = tmp_path / 'run'
    code = tmp_path / 'code'
    run.mkdir()
    code.mkdir()
    (code / 'plan.json').write_text(json.dumps({
        'source_id': 'source1', 'spec': {}, 'schema_labels': ['amount'],
        'schema_names': ['c_1'],
    }), encoding='utf-8')
    (run / 'sources.json').write_text(json.dumps({'source1': {'id': 'source1'}}), encoding='utf-8')
    monkeypatch.setattr(rules, 'validate_legacy_numeric', lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, 'read_source', lambda *args, **kwargs: (
        [{'c_1': '118.15'}], [{'name': 'c_1', 'label': 'amount'}], {}, None))

    class FailedInfo:
        def raise_on_failed_jobs(self):
            raise DestinationHasFailedJobs('duckdb', 'synthetic', [])

    class Pipeline:
        def run(self, resources):
            return FailedInfo() if result == 'failed_job' else None

    monkeypatch.setattr(runner.dlt, 'pipeline', lambda **kwargs: Pipeline())
    with pytest.raises(TaskError) as caught:
        runner.ingest(run, code)
    assert caught.value.code == 'DLT_LOAD_INCOMPLETE'
    assert not (run / 'input.json').exists()
    assert not (run / 'warehouse.duckdb').exists()


@pytest.mark.parametrize('remove_row', [False, True])
def test_real_dlt_checks_each_source_and_allows_empty_table(tmp_path, monkeypatch, remove_row):
    import duckdb

    run = tmp_path / 'run'
    code = tmp_path / 'code'
    run.mkdir();code.mkdir()
    fields=[{'name':'c_1','label':'amount'}]
    source_rows={
        'primary':[{'c_1':'100.01','record_hash':'p1'},{'c_1':'18.14','record_hash':'p2'}],
        'lookup':[{'c_1':'4.00','record_hash':'l1'},{'c_1':'5.00','record_hash':'l2'}],
        'empty':[],
    }
    joins=[{'source_id':name,'table':name,'schema_labels':['amount'],'fields':[{'name':'c_1'}]}
           for name in ('lookup','empty')]
    (code/'plan.json').write_text(json.dumps({'source_id':'primary','spec':{'allow_empty_source':True},
        'schema_labels':['amount'],'schema_names':['c_1'],'joins':joins}),encoding='utf-8')
    (run/'sources.json').write_text(json.dumps({name:{'id':name} for name in source_rows}),encoding='utf-8')
    monkeypatch.setattr(rules,'validate_legacy_numeric',lambda *args,**kwargs:None)
    monkeypatch.setattr(runner,'read_source',lambda source,**kwargs:(source_rows[source['id']],fields,{},None))
    monkeypatch.setenv('DLT_TELEMETRY','false')
    monkeypatch.setenv('DO_NOT_TRACK','1')
    if remove_row:
        original=runner.dlt.pipeline
        class DropAfterSuccessfulLoad:
            def __init__(self,pipe):self.pipe=pipe
            def run(self,resources):
                info=self.pipe.run(resources)
                with duckdb.connect(str(run/'warehouse.duckdb')) as con:
                    con.execute("delete from raw.lookup where c_1='4.00'")
                return info
        monkeypatch.setattr(runner.dlt,'pipeline',lambda **kwargs:DropAfterSuccessfulLoad(original(**kwargs)))
        with pytest.raises(TaskError) as caught:runner.ingest(run,code)
        assert caught.value.code=='DLT_LOAD_INCOMPLETE'
        assert not (run/'input.json').exists()
    else:
        runner.ingest(run,code)
        with duckdb.connect(str(run/'warehouse.duckdb'),read_only=True) as con:
            assert [con.execute('select count(*) from raw.'+name).fetchone()[0] for name in ('records','lookup','empty')]==[2,2,0]
        manifest=json.loads((run/'input.json').read_text(encoding='utf-8'))
        assert [item['records'] for item in manifest['sources']]==[2,2,0]
