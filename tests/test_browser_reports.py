"""Browser adapter contract tests. No real report website or account is used."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_service.browser_reports import (BrowserRecipe, BrowserReportError, BrowserUnavailable,
                                           availability, export_with_page)


class Locator:
    def __init__(self, owner, selector, *, value='', text='', kind='text', count=1):
        self.owner=owner; self.selector=selector; self.value=value; self.text=text; self.kind=kind; self.n=count
        self.ignore_fill=False
    def count(self):return self.n
    def wait_for(self, **kwargs):self.owner.events.append(('wait',self.selector))
    def get_attribute(self,name,**kwargs):return self.kind if name=='type' else None
    def input_value(self,**kwargs):self.owner.events.append(('read',self.selector));return self.value
    def select_option(self,*,value,**kwargs):
        self.owner.events.append(('select',self.selector,value))
        if not self.ignore_fill:self.value=value
    def fill(self,value,**kwargs):
        self.owner.events.append(('fill',self.selector,value))
        if not self.ignore_fill:self.value=value
    def inner_text(self,**kwargs):return self.text
    def click(self,**kwargs):
        self.owner.events.append(('click',self.selector))
        if self.selector=='#export':
            assert self.owner.listening,'export click preceded event listener'
            self.owner.export_clicks+=1
        if self.selector=='#query' and self.owner.reset_after_query:
            self.owner.frame.controls['#period'].value='old-period'


class Scope:
    def __init__(self,owner):self.owner=owner;self.controls={}
    def locator(self,selector):
        return self.controls.get(selector,Locator(self.owner,selector,count=0))


class Download:
    def __init__(self,content=b'id,amount\nA,10\nB,20\n'):
        self.content=content;self.error=None;self.saved=0
    def failure(self):return self.error
    def save_as(self,path):Path(path).write_bytes(self.content);self.saved+=1


class Page(Scope):
    def __init__(self):
        super().__init__(self);self.events=[];self.url='about:blank';self.listening=False;self.export_clicks=0
        self.frame=Scope(self);self.download=Download();self.reset_after_query=False
        self.controls['#report']=Locator(self,'#report',kind='iframe')
        self.controls['#period']=Locator(self,'#root-period',value='ROOT-DECOY')
        self.frame.controls['#period']=Locator(self,'#period',value='old-period')
        self.frame.controls['#query']=Locator(self,'#query',kind='button')
        self.frame.controls['#export']=Locator(self,'#export',kind='button')
        self.frame.controls['#context']=Locator(self,'#context',text='收入报告 2026-10 金额单位 CNY')
        self.frame.controls['#ready']=Locator(self,'#ready')
    def goto(self,url,**kwargs):self.url=url;self.events.append(('goto','redacted'))
    def frame_locator(self,selector):assert selector=='#report';return self.frame
    @contextmanager
    def expect_download(self,**kwargs):
        self.events.append(('listen',));self.listening=True
        try:yield SimpleNamespace(value=self.download)
        finally:self.listening=False;self.events.append(('listener_exit',))


def recipe(**changes):
    value={'url':'https://reports.example.test/report?temporary_token=do-not-record',
           'iframe_selector':'#report','filters':[{'label':'期间','selector':'#period','kind':'select','value':'2026-10'}],
           'query_button_selector':'#query','export_selector':'#export','context_selector':'#context',
           'required_context_text':['2026-10','CNY'],'ready_selector':'#ready'}
    return BrowserRecipe.model_validate(dict(value,**changes))


def validator(path):
    raw=path.read_bytes()
    if raw.lstrip().lower().startswith(b'<html'):raise ValueError('login page is not CSV')
    assert raw.startswith(b'id,amount\n')
    return {'content_sha256':hashlib.sha256(raw).hexdigest(),'format':'csv','rows':2,
            'filters_verified':False,'context_verified':False}


def export(page,output,**kwargs):
    return export_with_page(page,recipe(),output,validator=validator,network_policy_verified=True,**kwargs)


def test_scoped_filters_listener_order_complete_atomic_export_and_reuse(tmp_path):
    page=Page();path=tmp_path/'report.csv'
    first=export(page,path)
    assert first['browser_filters_verified'] is True
    assert first['validation']['filters_verified'] is False
    assert first['validation']['context_verified'] is False
    assert page.controls['#period'].value=='ROOT-DECOY'
    assert page.frame.controls['#period'].value=='2026-10'
    assert page.events.index(('listen',))<page.events.index(('click','#export'))
    assert first['content_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert not list(tmp_path.glob('*.part-*'))
    assert 'do-not-record' not in json.dumps(first)
    assert '2026-10' not in json.dumps(first)
    before=list(page.events)
    second=export(page,path)
    assert second['reused'] and page.events==before and page.export_clicks==1


@pytest.mark.parametrize('failure', ['filter','rerender','context','ambiguous'])
def test_bad_filter_context_or_locator_never_clicks_export(tmp_path,failure):
    page=Page()
    if failure=='filter':page.frame.controls['#period'].ignore_fill=True
    if failure=='rerender':page.reset_after_query=True
    if failure=='context':page.frame.controls['#context'].text='other report'
    if failure=='ambiguous':page.frame.controls['#period'].n=2
    with pytest.raises(BrowserReportError):export(page,tmp_path/'report.csv')
    assert page.export_clicks==0
    assert not (tmp_path/'report.csv').exists()


@pytest.mark.parametrize('failure',['failed','empty','oversize','html','bad_hash'])
def test_download_failure_or_invalid_content_leaves_no_final_file(tmp_path,failure):
    page=Page();output=tmp_path/'report.csv';r=recipe()
    if failure=='failed':page.download.error='upstream cancelled'
    if failure=='empty':page.download.content=b''
    if failure=='oversize':r=recipe(max_bytes=1)
    if failure=='html':page.download.content=b'<html>login token=secret</html>'
    validate=(lambda p:{'content_sha256':'0'*64}) if failure=='bad_hash' else validator
    with pytest.raises(BrowserReportError):
        export_with_page(page,r,output,validator=validate,network_policy_verified=True)
    assert page.export_clicks==1
    assert not output.exists()
    assert not output.with_name(output.name+'.browser-receipt.json').exists()
    assert not list(tmp_path.glob('*.part-*'))


def test_existing_unconfirmed_or_changed_file_never_reexports(tmp_path):
    page=Page();output=tmp_path/'report.csv'
    output.write_bytes(b'unknown result')
    with pytest.raises(BrowserReportError,match='完整回执'):export(page,output)
    assert page.export_clicks==0
    output.unlink();export(page,output)
    output.write_bytes(b'changed')
    with pytest.raises(BrowserReportError,match='不一致'):export(page,output)
    assert page.export_clicks==1


def test_browser_adapter_is_not_enabled_by_dependency_or_boolean_like_values(tmp_path):
    assert availability()['ready'] is False
    for configured in (False,None,'true',1):
        with pytest.raises(BrowserUnavailable):
            export_with_page(Page(),recipe(),tmp_path/'report.csv',validator=validator,network_policy_verified=configured)


def test_redirect_and_password_input_are_rejected_before_export(tmp_path):
    page=Page()
    def redirect(*a,**kw):page.url='https://login.example.test/login?secret=x'
    page.goto=redirect
    with pytest.raises(BrowserReportError,match='跳转'):export(page,tmp_path/'redirect.csv')
    assert page.export_clicks==0
    page=Page();page.frame.controls['#period'].kind='password'
    with pytest.raises(BrowserReportError,match='筛选输入'):export(page,tmp_path/'secret.csv')
    assert not any(e[0]=='select' for e in page.events)
    assert page.export_clicks==0


def test_recipe_rejects_malformed_scope_controls_and_context():
    with pytest.raises(ValueError):recipe(url='https://user:secret@reports.example.test/report')
    with pytest.raises(ValueError):recipe(required_context_text=[''])
    with pytest.raises(ValueError):recipe(filters=[{'label':'日期','selector':'#date','kind':'date','value':'next week'}])
    controls=[{'label':'期间','selector':'#period','kind':'select','value':'2026-10'}]*2
    with pytest.raises(ValueError):recipe(filters=controls)


def test_target_symlinks_are_rejected(tmp_path):
    actual=tmp_path/'actual';actual.mkdir();alias=tmp_path/'alias';alias.symlink_to(actual,target_is_directory=True)
    with pytest.raises(ValueError,match='符号链接'):export(Page(),alias/'report.csv')
