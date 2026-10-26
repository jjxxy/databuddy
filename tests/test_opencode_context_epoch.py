"""Only completed native compactions retire recovery coverage."""
import copy

import pytest

from agent_service import opencode_runtime as runtime
from agent_service.store import get,patch
from test_opencode_agent_bridge import turn


def message(identity='cmp_one',**fields):
    return {'id':identity,'type':'compaction','status':'completed','summary':'A real nonempty native summary',**fields}


@pytest.mark.parametrize('value',[
    message(status='running'),message(status='failed'),message(summary=''),
    message(summary='  '),message(summary=None),message(type='assistant'),
    message(id=''),{'type':'compaction','status':'completed'},'not a message',
])
def test_admission_running_failed_or_empty_summary_never_changes_epoch(turn,value):
    patch('turn',turn['id'],opencode_session_id='ses_epoch',model_usage={'opencode_compaction_calls':999})
    before=copy.deepcopy(get('turn',turn['id']))
    assert runtime._observe_completed_compactions(turn['id'],'ses_epoch',[value])==0
    assert get('turn',turn['id'])==before


def test_completed_summary_advances_once_and_stale_poll_cannot_advance_again(turn):
    patch('turn',turn['id'],opencode_session_id='ses_epoch')
    assert runtime._observe_completed_compactions(turn['id'],'ses_epoch',[message()])==1
    assert get('turn',turn['id'])['opencode_context_epoch']==1
    assert runtime._observe_completed_compactions(turn['id'],'ses_epoch',[message(),message()])==0
    assert runtime._observe_completed_compactions(turn['id'],'ses_epoch',[message(),message('cmp_two')])==1
    assert get('turn',turn['id'])['opencode_context_epoch']==2
    assert runtime._observe_completed_compactions(turn['id'],'ses_epoch',[message()])==0
    assert get('turn',turn['id'])['opencode_context_epoch']==2
    assert 'summary' not in str(get('turn',turn['id'])['opencode_context_compactions'])


@pytest.mark.parametrize('changes',[{'opencode_session_id':'retired_session'},
                                  {'state':'done','outcome':'cancelled'}])
def test_retired_session_or_cancelled_task_cannot_update_epoch(turn,changes):
    patch('turn',turn['id'],opencode_session_id='ses_epoch',opencode_context_epoch=3,**{
        key:value for key,value in changes.items() if key!='opencode_session_id'})
    if 'opencode_session_id' in changes:patch('turn',turn['id'],**changes)
    before=copy.deepcopy(get('turn',turn['id']))
    assert runtime._observe_completed_compactions(turn['id'],'ses_epoch',[message()])==0
    assert get('turn',turn['id'])==before


def test_new_session_is_independent_and_observed_summary_ids_are_bounded(turn):
    patch('turn',turn['id'],opencode_session_id='ses_epoch',opencode_context_epoch=1,
          opencode_context_compactions={'session_id':'old_session','ids':['cmp_one']})
    messages=[message('cmp_'+str(index)) for index in range(300)]
    assert runtime._observe_completed_compactions(turn['id'],'ses_epoch',messages)==300
    current=get('turn',turn['id'])
    assert current['opencode_context_epoch']==301
    assert len(current['opencode_context_compactions']['ids'])==256
    assert runtime._observe_completed_compactions(turn['id'],'ses_epoch',messages)==0
    assert runtime._observe_completed_compactions(turn['id'],'ses_epoch',[messages[-1]])==0
