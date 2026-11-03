"""A broken stored turn cannot stop unrelated projects or overtake its peers."""
from contextlib import nullcontext
from agent_service import conversation, workspace, tasks, provider


def test_corrupt_pending_turn_blocks_only_its_project(monkeypatch, caplog):
    turns=[
        {'id':'t_broken','project_id':'p_blocked','state':'agent_tool','agent_trace':[{'status':'started'}]},
        {'id':'t_later','project_id':'p_blocked','state':'queued','created_at':'2026-01-01T00:00:00+00:00'},
        {'id':'t_good','project_id':'p_other','state':'queued','created_at':'2026-01-01T00:00:01+00:00'},
    ]
    by_id={turn['id']:turn for turn in turns};handled=[]
    monkeypatch.setattr(conversation,'all_items',lambda kind:turns if kind=='turn' else [])
    monkeypatch.setattr(conversation,'get',lambda kind,ident:by_id[ident])
    monkeypatch.setattr(conversation,'patch',lambda kind,ident,**values:by_id[ident].update(values) or by_id[ident])
    monkeypatch.setattr(conversation,'handle',lambda turn:handled.append(turn['id']))
    monkeypatch.setattr(workspace,'begin',lambda turn:None)
    monkeypatch.setattr(workspace,'finalize',lambda turn:None)
    monkeypatch.setattr(workspace,'recover',lambda:None)
    monkeypatch.setattr(tasks,'ensure',lambda turn:None)
    monkeypatch.setattr(provider,'task_scope',lambda turn_id:nullcontext())
    monkeypatch.setattr(conversation,'_last_invalid_turn_warning',0.0)
    assert conversation.tick()
    assert handled==['t_good']
    assert by_id['t_broken']['state']=='agent_tool'
    assert by_id['t_later']['state']=='queued'
    conversation.tick()
    assert sum('Invalid pending conversation record' in record.message for record in caplog.records)==1


def test_corrupt_turn_appearing_while_waiting_for_project_lock_blocks_work(monkeypatch):
    valid={'id':'t_waiting','project_id':'p_race','state':'queued','created_at':'2026-01-01T00:00:00Z'}
    broken={'id':'t_broken_race','project_id':'p_race','state':'agent_tool'}
    calls=[];handled=[]
    def read(kind):
        if kind!='turn':return []
        calls.append(1)
        return [valid] if len(calls)<3 else [valid,broken]
    monkeypatch.setattr(conversation,'all_items',read)
    monkeypatch.setattr(conversation,'get',lambda kind,ident:valid)
    monkeypatch.setattr(conversation,'handle',lambda turn:handled.append(turn))
    monkeypatch.setattr(workspace,'recover',lambda:None)
    assert conversation.tick() is False
    assert handled==[]


def test_non_hashable_state_is_quarantined_and_unknown_record_blocks_global_queue(monkeypatch):
    bad={'id':'t_bad_type','project_id':'p_bad_type','state':['agent_tool'],'created_at':'2026-01-01T00:00:00Z'}
    good={'id':'t_good_type','project_id':'p_good_type','state':'queued','created_at':'2026-01-02T00:00:00Z'}
    turns,blocked=conversation._pending_turns([bad,good])
    assert blocked=={'p_bad_type'} and turns==[good]
    assert conversation._pending_turns([['invalid row'],good])[1]=={None}
    monkeypatch.setattr(conversation,'all_items',lambda kind:[['invalid row'],good])
    monkeypatch.setattr(workspace,'recover',lambda:None)
    monkeypatch.setattr(conversation,'handle',lambda turn: (_ for _ in ()).throw(AssertionError('must not run')))
    assert conversation.tick() is False


def test_fifo_uses_absolute_time_and_invalid_timestamps_are_quarantined():
    earlier={'id':'early','project_id':'p','state':'queued','created_at':'2026-01-01T09:00:00+08:00'}
    later={'id':'later','project_id':'p','state':'queued','created_at':'2026-01-01T02:00:00Z'}
    turns,blocked=conversation._pending_turns([later,earlier])
    assert [turn['id'] for turn in turns]==['early','later'] and not blocked
    for value in ('garbage','2026-01-01T00:00:00',None):
        _,blocked=conversation._pending_turns([dict(earlier,created_at=value)])
        assert blocked=={'p'}
