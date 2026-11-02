"""Slow model reasoning must not be mistaken for a 45-second failure."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_service import opencode_runtime as runtime, provider
from agent_service.store import get, patch
from test_opencode_runtime import _prepare
from test_opencode_idle_recovery import session
from test_opencode_agent_bridge import turn


def _settings(root,entry):
    config=json.loads((root/'config/opencode/opencode.json').read_text())
    return config['providers'][runtime._identity(entry)]['settings']


def test_real_generated_supervisor_config_gives_reasoning_time(monkeypatch,tmp_path):
    for name in ('NEXUS_OPENCODE_MODEL_TIMEOUT_SECONDS','NEXUS_OPENCODE_MODEL_IDLE_TIMEOUT_SECONDS'):
        monkeypatch.delenv(name,raising=False)
    root,entry,_=_prepare(monkeypatch,tmp_path)
    cfg=_settings(root,entry)
    assert 'timeout' not in cfg  # Active streams have no default total deadline.
    assert cfg['headerTimeout']==cfg['chunkTimeout']==900000
    monkeypatch.setenv('NEXUS_OPENCODE_MODEL_TIMEOUT_SECONDS','1200')
    monkeypatch.setenv('NEXUS_OPENCODE_MODEL_IDLE_TIMEOUT_SECONDS','420')
    root,entry,_=_prepare(monkeypatch,tmp_path)
    assert {k:_settings(root,entry)[k] for k in ('timeout','headerTimeout','chunkTimeout')}=={
        'timeout':1200000,'headerTimeout':420000,'chunkTimeout':420000}
    config=json.loads((root/'config/opencode/opencode.json').read_text())
    assert config['providers'][runtime._identity(entry)]['compatibility']['reasoningField']=='reasoning_content'


@pytest.mark.parametrize('total,idle',[('0','100'),('nope','100'),('100','-1'),('86401','1')])
def test_invalid_timeout_configuration_fails_closed(monkeypatch,total,idle):
    monkeypatch.setenv('NEXUS_OPENCODE_MODEL_TIMEOUT_SECONDS',total)
    monkeypatch.setenv('NEXUS_OPENCODE_MODEL_IDLE_TIMEOUT_SECONDS',idle)
    with pytest.raises(ValueError):runtime._upstream_timeouts()


def test_old_independent_service_receives_same_timeout_configuration(monkeypatch):
    path=Path(__file__).resolve().parents[1]/'deploy/opencode_bootstrap.py'
    spec=importlib.util.spec_from_file_location('fixture_oc_bootstrap',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.delenv('NEXUS_OPENCODE_MODEL_TIMEOUT_SECONDS',raising=False)
    monkeypatch.delenv('NEXUS_OPENCODE_MODEL_IDLE_TIMEOUT_SECONDS',raising=False)
    assert 'timeout' not in module.config('https://example.test')['providers']['nexus']['settings']
    monkeypatch.setenv('NEXUS_OPENCODE_MODEL_TIMEOUT_SECONDS','900')
    monkeypatch.setenv('NEXUS_OPENCODE_MODEL_IDLE_TIMEOUT_SECONDS','350')
    provider_config=module.config('https://example.test')['providers']['nexus']
    assert {k:provider_config['settings'][k] for k in ('timeout','headerTimeout','chunkTimeout')}=={
        'timeout':900000,'headerTimeout':350000,'chunkTimeout':350000}
    assert provider_config['compatibility']['reasoningField']=='reasoning_content'
    monkeypatch.setenv('NEXUS_OPENCODE_MODEL_TIMEOUT_SECONDS','0')
    with pytest.raises(ValueError):module.config('https://example.test')


def test_prompt_admission_slow_but_eventually_arrives_without_retry(turn,monkeypatch):
    monkeypatch.delenv('NEXUS_OPENCODE_ADMISSION_TIMEOUT_SECONDS',raising=False)
    clock=[0]; monkeypatch.setattr(runtime,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    def advance(_metadata,number):
        if number==1:clock[0]=60
    requests,events=session(monkeypatch,[[{'type':'idle'}],
        [{'id':'msg_prompt_1','type':'user'}, {'id':'idle_final','type':'idle','outcome':'failed'}]],on_context=advance)
    with pytest.raises(provider.ModelServiceError,match='模型服务未完成'):
        runtime.run_session(get('turn',turn['id']),'work',{},lambda *_:pytest.fail('no tool call'))
    assert len(events)==2
    assert len([r for r in requests if r[1].endswith('/prompt')])==1


def test_prompt_admission_configurable_timeout_and_no_automatic_replay(turn,monkeypatch):
    monkeypatch.setenv('NEXUS_OPENCODE_ADMISSION_TIMEOUT_SECONDS','75')
    clock=[0];monkeypatch.setattr(runtime,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    requests,_=session(monkeypatch,[[{'type':'idle'}]],on_context=lambda *_:clock.__setitem__(0,76))
    with pytest.raises(provider.ModelServiceError,match='会话未开始处理'):
        runtime.run_session(get('turn',turn['id']),'work',{},lambda *_:pytest.fail('no tool call'))
    assert len([r for r in requests if r[1].endswith('/prompt')])==1


def test_compaction_wait_uses_separate_long_timeout(turn,monkeypatch):
    monkeypatch.setenv('NEXUS_OPENCODE_COMPACTION_TIMEOUT_SECONDS','500')
    patch('turn',turn['id'],opencode_session_id='ses_idle',opencode_memory_generation=0)
    monkeypatch.setattr('agent_service.memory_service.generation',lambda *_:0)
    session(monkeypatch,[[]],reused=True)
    monkeypatch.setattr(runtime,'_needs_compaction',lambda *_:True)
    waits=[]
    def compact(*args,**kwargs):
        waits.append(kwargs['timeout']);raise RuntimeError('stop after observing compaction')
    monkeypatch.setattr(runtime,'compact_session',compact)
    with pytest.raises(RuntimeError,match='stop after observing'):
        runtime.run_session(get('turn',turn['id']),'work',{},lambda *_:None)
    assert waits==[500]


def test_invalid_admission_configuration_is_rejected_before_prompt(turn,monkeypatch):
    monkeypatch.setenv('NEXUS_OPENCODE_ADMISSION_TIMEOUT_SECONDS','0')
    requests,_=session(monkeypatch,[])
    with pytest.raises(ValueError,match='NEXUS_OPENCODE_ADMISSION_TIMEOUT_SECONDS'):
        runtime.run_session(get('turn',turn['id']),'work',{},lambda *_:pytest.fail('no tool call'))
    assert not any(method=='POST' and path.endswith('/prompt') for method,path,_ in requests)


def test_native_context_polling_backs_off_only_when_unchanged():
    poller=runtime._ContextPoller();activity=(0,0);messages=[{'id':'prompt','type':'user'}]
    seen=[]
    for tick in range(1,201):
        if poller.due(tick,activity):
            seen.append(tick);poller.recorded(tick,activity,messages)
    # Starts promptly, then stable snapshots move from 0.75s to 2.1s and
    # finally no more than about 5.1s between reads.
    assert seen[:4]==[1,6,11,16]
    assert len(seen)<15
    assert max(b-a for a,b in zip(seen,seen[1:]))<=34
    assert poller.due(seen[-1]+1,(1,0))  # actual model/tool activity wakes it
    poller.recorded(seen[-1]+1,(1,0),messages)
    assert poller.next_tick==seen[-1]+6
    assert not poller.due(seen[-1]+2,(1,0))
    poller.wake();assert poller.due(seen[-1]+2,(1,0))


def test_native_context_polling_change_resets_backoff():
    poller=runtime._ContextPoller(); activity=(1,0);tick=0
    for _ in range(8):
        tick=poller.next_tick
        poller.recorded(tick,activity,[{'id':'assistant','content':'unchanged'}])
    assert poller.next_tick-tick==34
    tick=poller.next_tick
    poller.recorded(tick,activity,[{'id':'assistant','content':'new streamed result'}])
    assert poller.next_tick-tick==5


def test_stopped_turn_is_seen_during_context_poll_backoff(turn,monkeypatch):
    # A long context-read interval must not become the state/cancellation
    # interval: the next loop sees the stored stop even when no poll is due.
    original=runtime._ContextPoller.recorded
    def long_wait(self,tick,activity,messages):
        original(self,tick,activity,messages)
        self.next_tick=tick+34
    monkeypatch.setattr(runtime._ContextPoller,'recorded',long_wait)
    def stop_turn(_metadata,number):
        if number==1:patch('turn',turn['id'],state='done',answer='已停止')
    _,events=session(monkeypatch,[[{'id':'msg_prompt_1','type':'user'}]],on_context=stop_turn)
    result=runtime.run_session(get('turn',turn['id']),'work',{},lambda *_:pytest.fail('no tool call'))
    assert result['state']=='done' and len(events)==1
