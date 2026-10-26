"""Only verified, structurally selectable Excel inputs may reach inspection."""
from pathlib import Path
import pytest

from agent_service import conversation as chat, planner
from agent_service.connectors import verified_file_source
from agent_service.store import get,patch
from test_multi_file_conversation import project,upload,submit,ready

BOOK=Path(__file__).resolve().parents[1]/'acceptance/finance_monthly/inputs/phase1/2026-09_业务源表.xlsx'

@pytest.mark.parametrize('code',['HEADER_SELECTION_REQUIRED','SHEET_SELECTION_REQUIRED','WORKBOOK_MERGE_AMBIGUOUS'])
def test_layout_choice_reaches_agent_without_marking_source_ready(project,monkeypatch,code):
    first=upload(project)['id'];second=upload(project,'layout.xlsx',BOOK.read_bytes())['id']
    ready([first]);patch('source',second,status='failed',error='明确读取边界',error_code=code)
    turn=submit(project,[first,second]);seen=[]
    monkeypatch.setattr('agent_service.agent.advance',lambda value:seen.append(value))
    project[2].clear();chat.handle(turn)
    assert len(seen)==1 and seen[0]['source_ids']==[first,second]
    assert get('source',second)['status']=='failed'
    assert project[2]==[]  # No repeated probe with the same ambiguous defaults.

@pytest.mark.parametrize('code',['WORKBOOK_INVALID','WORKBOOK_ENCRYPTED','WORKBOOK_LIMIT','FORMULA_CACHE_MISSING',None])
def test_unreadable_workbook_does_not_reach_model(project,monkeypatch,code):
    source=upload(project,'invalid.xlsx',BOOK.read_bytes())['id']
    patch('source',source,status='failed',error='unreadable',error_code=code)
    turn=submit(project,[source])
    monkeypatch.setattr('agent_service.agent.advance',lambda value:pytest.fail('Unreadable input reached model'))
    chat.handle(turn)
    assert get('turn',turn['id'])['outcome']=='needs_input'

def test_structural_code_does_not_bypass_original_content_integrity(project,monkeypatch):
    source=upload(project,'layout.xlsx',BOOK.read_bytes())['id']
    path=verified_file_source(get('source',source))[0];path.write_bytes(b'changed original')
    patch('source',source,status='failed',error='选择表头',error_code='HEADER_SELECTION_REQUIRED')
    turn=submit(project,[source])
    monkeypatch.setattr('agent_service.agent.advance',lambda value:pytest.fail('Changed source reached model'))
    chat.handle(turn)
    assert get('turn',turn['id'])['outcome']=='needs_input'

def test_successful_reprobe_clears_old_error_classification(project):
    source=upload(project)['id']
    patch('source',source,status='failed',error='old error',error_code='HEADER_SELECTION_REQUIRED')
    planner.persist_node({'source':get('source',source),'profile':{'fields':[]}})
    value=get('source',source)
    assert value['status']=='ready' and 'error' not in value and 'error_code' not in value

def test_explicit_primary_layout_input_is_not_routed_back_to_legacy_loop(project,monkeypatch):
    source=upload(project,'primary-layout.xlsx',BOOK.read_bytes())['id']
    patch('source',source,status='failed',error='明确表头',error_code='HEADER_SELECTION_REQUIRED')
    turn=submit(project,[source],source_id=source);seen=[]
    monkeypatch.setattr('agent_service.agent.advance',lambda value:seen.append(value))
    monkeypatch.setattr(chat,'legacy_handle',lambda value:pytest.fail('Structured workbook returned to legacy loop'))
    chat.handle(turn)
    assert len(seen)==1 and seen[0]['source_ids']==[source]
    assert get('source',source)['status']=='failed'

def test_inspection_permission_does_not_waive_explicit_execution_boundaries(project):
    from agent_service import project_data
    from agent_service.workbook_reader import WorkbookReadError
    source=upload(project,'official-layout.xlsx',BOOK.read_bytes())['id']
    patch('source',source,status='failed',error='读取选区待明确',error_code='WORKBOOK_MERGE_AMBIGUOUS')
    assert chat._selectable_workbook(get('source',source))
    with pytest.raises(WorkbookReadError):
        project_data.read_input(project[0],project_data.InputBinding(source_id=source))
    rows,fields,evidence,provenance=project_data.read_input(project[0],project_data.InputBinding(
        source_id=source,options={'sheet':'成本底稿','header_rows':[5],'cell_range':'C5:I15','data_start_row':6}))
    assert len(rows)==10 and provenance
    assert get('source',source)['status']=='failed'
