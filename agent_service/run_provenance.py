"""A public, bounded view of facts recorded by a pipeline run.

The source snapshot can contain encrypted credentials and configuration.  It
must never be returned verbatim.  Missing history is left missing rather than
substituting the current source, environment, or output as historical proof.
"""

from datetime import datetime


def _text(value, limit=256):
    return value[:limit] if isinstance(value, str) else None


def _number(value):
    return value if type(value) is int and value >= 0 else None


def _trigger(pipeline_id, run):
    # Non-scheduled chat requests also use scheduled_key for idempotency.
    # Only the scheduler's pipeline-id + ISO timestamp key identifies a
    # scheduled cycle; unknown keys cannot be labelled as scheduled or manual.
    key = run.get('scheduled_key')
    prefix = pipeline_id + ':'
    if not isinstance(key, str) or not key.startswith(prefix):
        return 'other'
    try:
        stamp = datetime.fromisoformat(key[len(prefix):])
    except ValueError:
        return 'other'
    return 'scheduled' if stamp.utcoffset() is not None else 'other'


def describe(pipeline, run, manifest):
    """Describe only evidence actually bound to this exact pipeline run."""
    if (not isinstance(pipeline, dict) or not isinstance(run, dict)
            or not isinstance(manifest, dict)
            or not isinstance(pipeline.get('id'), str)
            or not isinstance(run.get('id'), str)
            or not isinstance(run.get('commit'), str)
            or run.get('target') != pipeline['id']
            or manifest.get('pipeline_id') != pipeline['id']
            or manifest.get('run_id') != run['id']
            or manifest.get('commit') != run['commit']):
        raise ValueError('运行记录与项目版本不一致，不能展示运行依据')

    frozen = run.get('source_snapshot')
    frozen = frozen if isinstance(frozen, dict) else {}
    inputs = []
    for item in manifest.get('inputs', []):
        if not isinstance(item, dict) or not isinstance(item.get('source_id'), str):
            continue
        source_id = item['source_id']
        revision = _number(item.get('revision', item.get('source_revision')))
        source = frozen.get(source_id)
        # Only use the frozen record when it belongs to this input and its
        # recorded revision matches.  Never use a later live source name.
        if (not isinstance(source, dict) or source.get('id') != source_id
                or source.get('project_id') != pipeline.get('project_id')
                or revision is None or source.get('revision') != revision):
            source = {}
        inputs.append({'source_id': source_id, 'name': _text(source.get('name'), 120),
                       'kind': _text(source.get('kind'), 40), 'revision': revision,
                       'collected_at': _text(item.get('collected_at'), 80),
                       'records': _number(item.get('records')),
                       'input_hash': _text(item.get('input_hash'), 128)})

    runtime = manifest.get('runtime') if isinstance(manifest.get('runtime'), dict) else {}
    runtime = {key: runtime[key] for key in ('timeout_seconds', 'max_attempts', 'freshness_minutes')
               if _number(runtime.get(key)) is not None}
    versions = manifest.get('versions') if isinstance(manifest.get('versions'), dict) else {}
    dependencies = {key: versions[key] for key in ('dlt', 'duckdb', 'dbt-core', 'dbt-duckdb', 'langgraph')
                    if isinstance(versions.get(key), str) and len(versions[key]) <= 80}
    checks = manifest.get('quality')
    quality = {'available': isinstance(checks, list), 'passed': 0, 'failed': 0, 'other': 0}
    for item in checks if isinstance(checks, list) else []:
        status = str(item.get('status', '')).lower() if isinstance(item, dict) else ''
        bucket = 'passed' if status in ('pass', 'passed', 'success') else 'failed' if status in ('fail', 'failed', 'error') else 'other'
        quality[bucket] += 1
    return {'run_id': run['id'], 'commit': run['commit'],
            'trigger': _trigger(pipeline['id'], run),
            'created_at': _text(run.get('created_at'), 80),
            'started_at': _text(run.get('started_at'), 80),
            'finished_at': _text(run.get('finished_at'), 80),
            'inputs': inputs, 'input_hash': _text(manifest.get('input_hash'), 128),
            'output_hash': _text(manifest.get('parquet_hash'), 128),
            'runtime': runtime, 'dependencies': dependencies, 'quality': quality,
            'identity_policy': _text(manifest.get('identity_policy'), 100),
            'replay': {'available': False, 'reason': '历史原始数据和完整运行环境尚未验证可重放'}}
