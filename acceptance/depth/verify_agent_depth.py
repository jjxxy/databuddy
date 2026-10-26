"""Opt-in actual model depth cases against an explicit app artifact."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import traceback

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from common import CaseContext
CASES=('finance_monthly','finance_negative','large_finance','complex_excel','relational_sales','dirty_inventory','multi_turn_hr')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root',type=Path,required=True)
    parser.add_argument('--model-config',type=Path,required=True)
    parser.add_argument('--evidence-dir',type=Path,required=True)
    parser.add_argument('--case',choices=CASES,required=True)
    parser.add_argument('--negative-case',choices=('N01','N02','N03','N04'),help='Run one official negative scenario; other scenarios remain unverified.')
    parser.add_argument('--timeout',type=int,default=600)
    args=parser.parse_args();app=args.app_root.resolve();evidence=args.evidence_dir.resolve()
    if args.negative_case and args.case!='finance_negative':parser.error('--negative-case requires --case finance_negative')
    if evidence.exists() and any(evidence.iterdir()):parser.error('Evidence directory must be new or empty')
    if not args.model_config.is_file():parser.error('Explicit model configuration path is missing')
    evidence.mkdir(parents=True,exist_ok=True)
    spec=importlib.util.spec_from_file_location('packaged_acceptance_host',app/'acceptance/offline/verify_backend.py')
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    report={'ok':False,'case':args.case,'app_root':str(app),'synthetic_data_only':True,
        'real_model_evaluated':False,'default_budgets_used':True,'model_configuration_contents_logged':False}
    started=time.monotonic();host=None;ctx=None
    with tempfile.TemporaryDirectory(prefix='atlas-depth-',ignore_cleanup_errors=True) as temporary:
        try:
            host=helper.Host(app,Path(temporary)/'state',args.model_config.resolve(),offline=False).start()
            ctx=CaseContext(host,evidence,args.case,args.timeout)
            ctx.negative_case=args.negative_case
            spec=importlib.util.spec_from_file_location('depth_case_'+args.case,HERE/'cases'/(args.case+'.py'))
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            result=module.run(ctx)
            if isinstance(result,dict):report.update(result)
            report['ok']=bool(result.get('passed'))
            report['real_model_evaluated']=ctx.record['real_model_evaluated']
            if not report['real_model_evaluated']:raise AssertionError('Case did not exercise a real model')
        except BaseException as error:
            frames=traceback.extract_tb(error.__traceback__)
            message=str(error)
            if host and host.token:message=message.replace(host.token,'[redacted]')
            report['failure']={'type':type(error).__name__,'message':message[:2000],
                'location':{'file':Path(frames[-1].filename).name,'line':frames[-1].lineno} if frames else None}
        finally:
            if ctx:
                ctx.save();report['real_model_evaluated']=ctx.record['real_model_evaluated']
                report['turns']=[{'state':t.get('state'),'outcome':t.get('outcome'),'model_usage':t.get('model_usage')} for t in ctx.turns]
            if host:
                try:host.stop()
                except Exception:report['shutdown_error']=True
    report['elapsed_seconds']=round(time.monotonic()-started,2)
    (evidence/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps({'ok':report['ok'],'case':args.case,'real_model_evaluated':report['real_model_evaluated'],
        'elapsed_seconds':report['elapsed_seconds'],'failure':report.get('failure'),'evidence':str(evidence)},ensure_ascii=False))
    return 0 if report['ok'] else 1

if __name__=='__main__':raise SystemExit(main())
