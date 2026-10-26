import hashlib
import pytest
from openpyxl import Workbook
from agent_service import project_data as data
from agent_service.config import DATA
from agent_service.store import put,seal,uid,now
from agent_service.workbook_reader import WorkbookReadError
from test_project_data import project


@pytest.mark.parametrize('hidden,expected',[(False,'SUBTOTAL_ROWS_PRESENT'),(True,'HIDDEN_ROWS_PRESENT')])
def test_workflow_requires_policy_but_readonly_preview_exposes_questionable_rows(hidden,expected):
    pid=project();book=Workbook();sheet=book.active
    sheet.append(['项目','金额']);sheet.append(['明细',10]);sheet.append(['合计',10])
    if hidden:sheet.row_dimensions[2].hidden=True
    name=uid()+'.xlsx';(DATA/'uploads').mkdir(exist_ok=True);book.save(DATA/'uploads'/name)
    source=put('source',{'project_id':pid,'kind':'file','name':'底稿.xlsx','status':'ready',
                        'content_hash':hashlib.sha256((DATA/'uploads'/name).read_bytes()).hexdigest(),
                        'secret_config':seal({'stored_name':name}),'created_at':now()},'s_'+uid())
    with pytest.raises(WorkbookReadError) as caught:data.read_input(pid,{'source_id':source['id']})
    assert caught.value.code==expected
    page=data.preview_source(pid,{'source_id':source['id']})
    assert len(page['rows'])==2 and page['sample_only'] and page['evidence']['requires_semantic_review']
    assert page['complete_source_read']
    rows,_,evidence,_=data.read_input(pid,{'source_id':source['id'],'options':{
        'hidden_rows':'include','subtotal_policy':'preserve'}})
    assert len(rows)==2 and evidence['requires_semantic_review']
    # Choosing only the actual detail range is a complete explicit input.
    rows,_,_,_=data.read_input(pid,{'source_id':source['id'],'options':{
        'cell_range':'A1:B2','hidden_rows':'include'}})
    assert len(rows)==1


def test_missing_formula_cache_is_inspectable_but_never_executable():
    pid=project();book=Workbook();sheet=book.active
    sheet.append(['项目','金额']);sheet.append(['明细','=1+1'])
    name=uid()+'.xlsx';(DATA/'uploads').mkdir(exist_ok=True);book.save(DATA/'uploads'/name)
    source=put('source',{'project_id':pid,'kind':'file','name':'公式.xlsx','status':'ready',
                        'content_hash':hashlib.sha256((DATA/'uploads'/name).read_bytes()).hexdigest(),
                        'secret_config':seal({'stored_name':name}),'created_at':now()},'s_'+uid())
    page=data.preview_source(pid,{'source_id':source['id']})
    assert page['rows'][0]['c_2'] is None and not page['complete_source_read']
    assert page['evidence']['requires_semantic_review']
    assert page['evidence']['special_cells']['2']['c_2']['formula']['formula']=='=1+1'
    with pytest.raises(WorkbookReadError) as caught:data.read_input(pid,{'source_id':source['id']})
    assert caught.value.code=='FORMULA_CACHE_MISSING'
