"""Verify formal provenance against frozen runs using the delivered backend.

Synthetic data only, no model configuration or AI calls. The host uses bundled
Windows runtimes and application local-only policy, not an OS firewall test.
"""
from __future__ import annotations

import argparse
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import traceback


spec=importlib.util.spec_from_file_location('lineage_acceptance_backend',
    Path(__file__).with_name('verify_backend.py'))
backend=importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)
ROOT=Path(__file__).resolve().parents[2]


def ready_candidate(host,pipeline_id,commit):
    def check():
        pipeline=next(value for value in host.json('GET','/api/pipelines')
                      if value['id']==pipeline_id)
        runs=host.json('GET','/api/runs?target='+pipeline_id)
        failed=[run for run in runs if run.get('commit')==commit
                and run.get('status') in ('failed','cancelled')]
        if failed:
            for run in failed:
                host.lines.append(json.dumps(host.json('GET','/api/runs/'+run['id']),
                                             ensure_ascii=False))
            raise AssertionError('Synthetic provenance pipeline did not validate.')
        return pipeline if pipeline.get('candidate_run') and pipeline['commit']==commit else None
    return backend.wait(host,check)


def verify(host):
    project=host.json('POST','/api/projects',{
        'name':'正式来源冻结验收','request_id':backend.identity()})
    fixtures=[b'id,customer,amount\nA,C1,10.25\nB,C2,20.50\n',
              b'customer,region\nC1,East\nC2,West\n',
              b'customer,region\nC1,North\nC2,South\n']
    uploaded=[]
    for payload in fixtures:
        status,value=host.upload(project['id'],payload)
        assert status==200
        uploaded.append(value['id'])
    def ready_sources():
        sources={value['id']:value for value in host.json('GET','/api/sources')}
        values=[sources.get(source_id) for source_id in uploaded]
        if any(value and value.get('status')=='failed' for value in values):
            raise AssertionError('Synthetic source profiling failed.')
        return values if all(value and value.get('status')=='ready' for value in values) else None
    main,original,revised=backend.wait(host,ready_sources)
    transform=dict(main['proposed_spec'],primary_key='c_1',group_by=None,amount_field=None,
        joins=[{'source_id':original['id'],'left_field':'c_2','right_field':'c_1',
                'label':'客户','required_match':True}])
    pipeline=host.json('POST','/api/pipelines',{'source_id':main['id'],
        'name':'冻结来源核对','spec':transform,'release_mode':'manual'})
    pipeline_id=pipeline['id'];route='/api/pipelines/'+pipeline_id
    assert host.call('GET',route+'/lineage',auth=False)[0]==401
    assert host.call('GET',route+'/lineage',token='invalid-synthetic-token')[0]==401
    first=ready_candidate(host,pipeline_id,pipeline['commit'])
    draft=host.json('GET',route+'/lineage')
    assert set(draft['source_ids'])=={main['id'],original['id']}
    assert 'run_id' not in draft and not first.get('published_run')
    preview=host.json('GET',route+'/preview')
    assert preview['manifest']['records']==2 and len(preview['rows'])==2
    assert sum((Decimal(row['c_3']) for row in preview['rows']),Decimal(0))==Decimal('30.75')
    assert {row['j1_c_2'] for row in preview['rows']}=={'East','West'}
    host.json('POST',route+'/publish',{'run_id':first['candidate_run']})
    frozen=host.json('GET',route+'/lineage')
    assert set(frozen['source_ids'])=={main['id'],original['id']}

    transform['joins'][0]['source_id']=revised['id']
    changed=host.json('PUT',route,{'source_id':main['id'],'name':'冻结来源核对',
        'spec':transform,'release_mode':'manual'})
    new=ready_candidate(host,pipeline_id,changed['commit'])
    assert set(new['source_ids'])=={main['id'],revised['id']}
    assert new['commit']!=first['commit']
    assert new['published_run']==first['candidate_run']
    assert new['candidate_run']!=first['candidate_run']
    candidate=host.json('GET',route+'/preview')
    assert {row['j1_c_2'] for row in candidate['rows']}=={'North','South'}
    formal=host.json('GET',route+'/lineage')
    assert formal['run_id']==first['candidate_run']
    assert formal['active_commit']==first['commit']
    assert formal['candidate_commit']==new['commit']
    assert formal['inputs']==frozen['inputs']
    assert set(formal['source_ids'])=={entry['source_id'] for entry in formal['inputs']}
    assert set(formal['source_ids'])=={main['id'],original['id']}
    assert revised['id'] not in formal['source_ids']
    published=host.json('GET',route+'/result')
    assert published['total']==2 and {row['j1_c_2'] for row in published['rows']}=={'East','West'}
    assert sum((Decimal(row['c_3']) for row in published['rows']),Decimal(0))==Decimal('30.75')
    assert host.json('GET','/api/audit/verify')['ok']
    return {'project_id':project['id'],'pipeline_id':pipeline_id,
        'fixture_sha256':[hashlib.sha256(payload).hexdigest() for payload in fixtures],
        'candidate_only_has_no_formal_result':True,'manual_publication_required':True,
        'formal_source_ids':sorted(formal['source_ids']),
        'candidate_source_ids':sorted(new['source_ids']),
        'formal_run_id':formal['run_id'],'formal_commit':formal['active_commit'],
        'candidate_commit':formal['candidate_commit'],
        'formal_rows':2,'exact_amount_total':'30.75',
        'formal_join_values':['East','West'],'candidate_join_values':['North','South'],
        'published_sources_match_frozen_inputs':True,'candidate_not_auto_published':True,
        'provenance_graph_api_contract':True,'unauthorized_requests_rejected':True,
        'audit_verified':True}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root',type=Path,default=ROOT)
    parser.add_argument('--evidence-dir',type=Path,required=True)
    args=parser.parse_args(argv)
    app,evidence=args.app_root.resolve(),args.evidence_dir.resolve()
    if evidence.exists() and any(evidence.iterdir()):parser.error('--evidence-dir must be new or empty')
    if sys.platform!='win32':parser.error('This acceptance harness targets native Windows only.')
    evidence.mkdir(parents=True,exist_ok=True)
    result={'ok':False,'app_root':str(app),'platform':'native Windows',
        'synthetic_data_only':True,'real_ai_evaluated':False,
        'native_ui_or_react_flow_visual_evaluated':False,
        'physical_network_disconnect_evaluated':False}
    hosts=[];step='startup'
    try:
        with backend.test_workspace(hosts) as temporary:
            host=backend.Host(app,Path(temporary)/'lineage-state');hosts.append(host)
            host.start();step='formal_run_and_revised_candidate'
            result['checks']=verify(host)
            step='clean_shutdown';host.stop()
            result.update(ok=True,offline_policy='local_only',inherited_path_empty=True,
                          model_configuration_required=False)
    except BaseException as error:
        frames=traceback.extract_tb(error.__traceback__)
        result['failure']={'step':step,'type':type(error).__name__,
            'location':{'file':Path(frames[-1].filename).name,'line':frames[-1].lineno} if frames else None}
        for index,host in enumerate(hosts):
            try:(evidence/f'lineage-diagnostic-{index}.log').write_text(host.diagnostic(),encoding='utf-8')
            except OSError:pass
    finally:
        for host in reversed(hosts):
            try:host.stop()
            except Exception:pass
    (evidence/'lineage-verification.json').write_text(
        json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
