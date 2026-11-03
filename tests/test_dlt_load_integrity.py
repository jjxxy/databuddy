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
