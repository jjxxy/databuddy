"""Compare effective-date matching with a separate linear-scan reference."""
import random
from datetime import date, timedelta

from agent_service.data_workflows import execute


def _column(name, data_type='text'):
    return {'name':name,'data_type':data_type,'meaning':name}


def _schema(columns, grain):
    return {'columns':columns,'grain':grain,'meaning':'测试数据'}


def test_effective_lookup_matches_independent_scan_across_entities_and_boundaries():
    plan={'name':'按生效期匹配', 'inputs':{
        'transactions':_schema([_column('id'),_column('entity'),_column('customer'),_column('day','date')],['id']),
        'rules':_schema([_column('rule_id'),_column('entity'),_column('customer'),_column('start','date'),
                         _column('end','date'),_column('owner')],['rule_id'])},
        'stages':[{'kind':'lookup_effective','id':'mapped','description':'按业务日匹配',
                   'input':'transactions','right':'rules',
                   'keys':[{'left':'entity','right':'entity'},{'left':'customer','right':'customer'}],
                   'business_date':'day','valid_from':'start','valid_to':'end',
                   'null_end_policy':'open','unmapped':'null',
                   'outputs':{'rule_id':_column('matched_rule_id'),
                              'start':_column('matched_start','date'),
                              'end':_column('matched_end','date'),
                              'owner':_column('matched_owner')}}], 'output':'mapped'}
    origin=date(2020,1,1)
    for seed in range(40):
        randomizer=random.Random(seed)
        rules=[];transactions=[];candidates=[]
        for entity in ('A','B','C'):
            for customer in ('C1','C2'):
                cursor=randomizer.randrange(5)
                count=randomizer.randrange(1,7)
                for version in range(count):
                    start=cursor+randomizer.randrange(3)
                    end=start+randomizer.randrange(1,7)
                    cursor=end
                    open_end=version==count-1 and randomizer.choice((True,False))
                    rules.append({'rule_id':f'{entity}-{customer}-{version}', 'entity':entity,
                                  'customer':customer,'start':(origin+timedelta(days=start)).isoformat(),
                                  'end':None if open_end else (origin+timedelta(days=end)).isoformat(),
                                  'owner':str(version)})
                    # Exercise the day before, first included day and the
                    # exclusive boundary, independently of random sampling.
                    for day in (start-1,start,end-1,end):
                        candidates.append((entity,customer,day))
        randomizer.shuffle(rules)
        randomizer.shuffle(candidates)
        candidates=candidates[:50]+[(randomizer.choice(('A','B','C','D')),
                                     randomizer.choice(('C1','C2','unknown')),
                                     randomizer.randrange(35)) for _ in range(35)]
        for index,(entity,customer,day) in enumerate(candidates):
            transactions.append({'id':str(index),'entity':entity,'customer':customer,
                                 'day':(origin+timedelta(days=day)).isoformat()})
        result=execute(plan,{'transactions':transactions,'rules':rules})
        missing=0
        for original,output in zip(transactions,result['output']['rows'],strict=True):
            # Independent exhaustive scan, not the production binary search.
            matches=[rule for rule in rules if rule['entity']==original['entity']
                     and rule['customer']==original['customer'] and rule['start']<=original['day']
                     and (rule['end'] is None or original['day']<rule['end'])]
            assert len(matches)<=1
            chosen=matches[0] if matches else None
            if chosen is None:missing+=1
            assert output['matched_rule_id']==(chosen['rule_id'] if chosen else None)
            assert output['matched_start']==(chosen['start'] if chosen else None)
            assert output['matched_end']==(chosen['end'] if chosen else None)
            assert output['matched_owner']==(chosen['owner'] if chosen else None)
        assert result['stages'][0]['checks']['unmatched_rows']==missing
        assert result['requires_semantic_review'] is True
