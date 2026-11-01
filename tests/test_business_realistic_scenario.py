"""A small, deliberately awkward synthetic finance case, not production evidence."""
from decimal import Decimal
from openpyxl import Workbook
import pytest
from agent_service.workbook_reader import read_table,WorkbookReadError
from agent_service.data_workflows import execute,WorkflowError


def col(name,typ='text',unit='',**kw):return {'name':name,'meaning':name,'data_type':typ,'unit':unit,**kw}
def schema(cols,grain):return {'meaning':'明细','columns':cols,'grain':grain}


def test_multisheet_merged_header_refunds_filter_and_missing_formula(tmp_path):
    book=Workbook();income=book.active;income.title='收入'
    for coln,label in [('A','订单'),('B','办事处'),('C','状态')]:
        income[f'{coln}1']=label;income.merge_cells(f'{coln}1:{coln}2')
    income['D1']='金额';income.merge_cells('D1:E1');income['D2']='收入（元）';income['E2']='说明'
    for row in [('A','东部','完成',100.25,'正常'),('B','东部','完成',100.25,'重复金额不可合并'),('C','西部','取消',20,'取消')]:income.append(row)
    refunds=book.create_sheet('退款');refunds.append(['订单','退款（元）']);refunds.append(['A',10.1]);refunds.append(['B',2]);refunds.append(['C',0])
    formula=book.create_sheet('公式');formula.append(['金额']);formula.append(['=1+1'])
    path=tmp_path/'收入与退款.xlsx';book.save(path)
    with pytest.raises(WorkbookReadError) as exc:read_table(path)
    assert exc.value.code=='SHEET_SELECTION_REQUIRED'
    with pytest.raises(WorkbookReadError) as exc:read_table(path,sheet='公式')
    assert exc.value.code=='FORMULA_CACHE_MISSING'
    left=read_table(path,sheet='收入',header_rows=[1,2]);right=read_table(path,sheet='退款',header_rows=[1])
    assert left['source_rows']==[3,4,5] and left['fields'][3]['unit']=='元'
    order=[{'id':r['c_1'],'office':r['c_2'],'status':r['c_3'],'amount':r['c_4']} for r in left['rows']]
    refund=[{'id':r['c_1'],'refund':r['c_2']} for r in right['rows']]
    plan={'name':'收入扣退款','inputs':{'orders':schema([col('id'),col('office'),col('status'),col('amount','decimal','元',aggregation='additive')],['id']),
          'refunds':schema([col('id'),col('refund','decimal','元',aggregation='additive')],['id'])},'stages':[
          {'id':'active','kind':'filter','description':'排除取消订单','input':'orders','conditions':[{'column':'status','operator':'eq','value':'完成'}]},
          {'id':'match','kind':'lookup','description':'匹配退款','input':'active','right':'refunds','left_key':'id','right_key':'id',
           'outputs':{'refund':col('refund','decimal','元',aggregation='additive')}},
          {'id':'net','kind':'compute','description':'扣退款','input':'match','calculations':[{'left':'amount','right':'refund','operation':'subtract','output':col('net','decimal','元',aggregation='additive')}]},
          {'id':'total','kind':'aggregate','description':'办事处汇总','input':'net','by':['office'],'meaning':'净收入',
           'aggregations':[{'function':'sum','column':'net','output':col('total','decimal','元')}] }], 'output':'total'}
    result=execute(plan,{'orders':order,'refunds':refund})
    assert result['output']['row_count']==1 and Decimal(result['output']['rows'][0]['total'])==Decimal('188.40')
    # Missing refunds and duplicate right-side keys cannot be invented as zero
    # or silently multiply financial records.
    with pytest.raises(WorkflowError):execute(plan,{'orders':order,'refunds':refund[:-2]})
    with pytest.raises(WorkflowError):execute(plan,{'orders':order,'refunds':refund+[dict(refund[0])]})
