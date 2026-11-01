"""Check shipped finance fixtures and the independent deploy-time export oracle.

These are fixture/parser checks, not a real-LLM agent evaluation.
"""
from copy import deepcopy
import csv
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from agent_service.workbook_reader import (WorkbookReadError, inspect_workbook,
                                           parse_localized_value, read_table)

KIT=Path(__file__).resolve().parents[1]/'acceptance'/'finance_monthly'


def module(name):
    spec=importlib.util.spec_from_file_location('finance_acceptance_'+name,KIT/(name+'.py'))
    result=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def verifier():
    return module('verify_export')


@pytest.fixture
def gold():
    return json.loads((KIT/'expected'/'gold.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('scenario,revenue,cost,profit,count',[
    ('baseline_sep','460001.30','239000.50','221000.80',7),
    ('baseline_oct','537001.75','270000.70','267001.05',7),
    ('corrected_oct','539001.75','270000.70','269001.05',7),
    ('include_rd_sep','460001.30','239778.27','220223.03',8),
])
def test_independent_gold_headlines(gold,scenario,revenue,cost,profit,count):
    total=next(r for r in gold[scenario] if r['办事处']=='全部')
    assert (total['收入元'],total['直接成本元'],total['毛利元'],total['成本笔数'])==(revenue,cost,profit,count)
    assert total['收入笔数']==7
    offices=[r for r in gold[scenario] if r['办事处']!='全部']
    for name in ['收入元','直接成本元','毛利元','预算收入元','预算成本元']:
        assert sum(Decimal(r[name]) for r in offices)==Decimal(total[name])
    assert abs(Decimal(total['毛利率'])-Decimal(profit)/Decimal(revenue))<Decimal('0.000000000001')


@pytest.mark.parametrize('month,phase,revenue,cost',[
    ('2026-09','phase1','460001.30','239000.50'),
    ('2026-10','phase2','537001.75','270000.70'),
])
def test_real_coordinate_reader_matches_independent_controls(month,phase,revenue,cost):
    path=KIT/'inputs'/phase/(month+'_业务源表.xlsx')
    document=inspect_workbook(path)
    assert [s['name'] for s in document['sheets']]==['收入底稿','成本底稿','预算','省份映射']
    # Explicit source-evidence rule: numeric percentage cells are already a
    # fraction; percent-sign text divides by 100. No bare numbers are inferred.
    sales=read_table(path,sheet='收入底稿',cell_range='B5:I15',header_rows=[5,6],data_start_row=7,data_end_row=15,
                     percentage_columns={'c_6':'fraction'})
    costs=read_table(path,sheet='成本底稿',cell_range='C5:I15',header_rows=[5],data_start_row=6,data_end_row=15)
    budget=read_table(path,sheet='预算',cell_range='A4:E7',header_rows=[4])
    mapping=read_table(path,sheet='省份映射',cell_range='A1:D8',header_rows=[1])
    assert sales['source_rows']==list(range(7,16))
    assert len(costs['rows'])==10
    assert sales['fields'][3]['label']=='收入信息 / 不含税收入'
    assert sales['fields'][4]['header_coordinates'][0]['source_coordinate']=='E5'
    assert sales['fields'][3]['excel_column']=='E'
    assert sales['rows'][1]['c_4']==('8.000005' if month=='2026-09' else '10.000005')
    assert all(r['c_6']==Decimal('.13') for r in sales['rows'])
    def normalized(value,unit):
        multiplier='10000' if unit=='万元' else '1'
        if isinstance(value,Decimal):
            return value*Decimal(multiplier)
        return parse_localized_value(value,kind='decimal',currency='CNY',unit=unit,unit_multiplier=multiplier)['value']
    actual_revenue=Decimal(0)
    for row in sales['rows']:
        if row['c_2'].replace('/','-')[:7]==month and row['c_7']=='已入账':
            actual_revenue+=normalized(row['c_4'],row['c_5'])
    actual_cost=sum((normalized(r['c_5'],r['c_6']) for r in costs['rows']
                     if r['c_2'].replace('/','-')[:7]==month and r['c_4'] in ['材料费','外包费'] and r['c_7']=='已入账'),Decimal(0))
    assert actual_revenue==Decimal(revenue)
    assert actual_cost==Decimal(cost)
    budget_controls={'2026-09':('480000','255000','225000'),'2026-10':('530000','278000','252000')}
    for column,control in zip(['c_2','c_3','c_5'],budget_controls[month]):
        assert sum(r[column] for r in budget['rows'])*10000==Decimal(control)
    knowledge=budget['special_cells']['5']['c_5']
    assert knowledge['formula']['formula']=='=B5-C5'
    assert knowledge['formula']['references']==['B5','C5']
    assert knowledge['formula']['parse_status']=='tokens_only_not_executed'
    assert knowledge['cache_freshness']=='unknown'
    definition=json.loads((KIT/'fixture_definition.json').read_text(encoding='utf-8'))
    assert {r['c_1']:r['c_2'] for r in mapping['rows']}==definition['province_office']


def test_formula_error_fixture_is_rejected_by_real_reader():
    with pytest.raises(WorkbookReadError) as exc:
        read_table(KIT/'inputs'/'negative'/'异常_公式.xlsx',sheet='不可核实公式',header_rows=[1],cell_range='A1:F2')
    assert exc.value.code=='FORMULA_CACHED_ERROR'


@pytest.mark.parametrize('scenario',['baseline_sep','baseline_oct','baseline_both','corrected_oct','include_rd_sep'])
def test_standard_csv_exports_compare_with_gold(verifier,gold,scenario):
    rows=verifier.load_export(KIT/'expected'/(scenario+'.csv'),list(gold[scenario][0]))
    assert verifier.compare(rows,gold[scenario])==[]


def test_export_checker_rejects_duplicate_and_missing_business_keys(verifier,gold):
    rows=deepcopy(gold['baseline_sep'])
    assert any('重复业务键' in e for e in verifier.compare(rows+[rows[0]],rows))
    assert any('缺少结果行' in e for e in verifier.compare(rows[:-1],rows))
    extra=deepcopy(rows[0]); extra['办事处']='未知办事处'
    assert any('多出结果行' in e for e in verifier.compare(rows+[extra],rows))


def test_nonempty_row_outside_expected_columns_is_not_silently_skipped(verifier,gold,tmp_path):
    expected=gold['baseline_sep'];headers=list(expected[0]);path=tmp_path/'result.csv'
    with path.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(headers+['备注'])
        for row in expected:writer.writerow([row[key] for key in headers]+[''])
        writer.writerow(['']*len(headers)+['未识别的另一笔业务'])
    with pytest.raises(ValueError,match='不能静默跳过'):
        verifier.load_export(path,headers)


@pytest.mark.parametrize('bad',['NaN','Infinity','460001.31','460,001.30','¥460001.30',
                                 '1e999999999','1e-999999999','460_001.30'])
def test_export_checker_rejects_wrong_or_nonatomic_money(verifier,gold,bad):
    rows=deepcopy(gold['baseline_sep'])
    rows[-1]['收入元']=bad
    assert verifier.compare(rows,gold['baseline_sep'])


def test_export_checker_rejects_percentage_strings_and_averages(verifier,gold):
    rows=deepcopy(gold['baseline_sep'])
    rows[-1]['毛利率']='48.04%'
    assert verifier.compare(rows,gold['baseline_sep'])
    rows[-1]['毛利率']=str(sum(Decimal(r['毛利率']) for r in rows[:-1])/3)
    assert verifier.compare(rows,gold['baseline_sep'])


def test_export_checker_reads_real_xlsx_strings_and_formula_failure(verifier):
    rows=verifier._xlsx_rows(KIT/'inputs'/'phase1'/'2026-09_业务源表.xlsx','收入底稿')
    assert any(len(r)>1 and r[1]=='S09001' for r in rows)
    assert next(r for r in rows if len(r)>1 and r[1]=='S09002')[4]=='8.000005'
    with pytest.raises(ValueError,match='含Excel错误'):
        verifier._xlsx_rows(KIT/'inputs'/'negative'/'异常_公式.xlsx','不可核实公式')


def test_excel_numeric_cells_remain_numeric_and_text_amounts_are_rejected(verifier,gold,tmp_path):
    from openpyxl import Workbook
    expected=gold['baseline_sep'];headers=list(expected[0]);path=tmp_path/'result.xlsx'
    book=Workbook();sheet=book.active;sheet.title='结果';sheet.append(headers)
    for row in expected:
        sheet.append([row[name] if name in verifier.KEYS else float(Decimal(row[name])) for name in headers])
    book.save(path)
    rows=verifier.load_export(path,headers,sheet='结果')
    assert verifier.compare(rows,expected)==[]
    # A numeric-looking string in Excel is still text; do not certify it as
    # a numeric result merely because Decimal can parse its characters.
    amount_column=headers.index('收入元')+1
    sheet.cell(2,amount_column).value=expected[0]['收入元'];book.save(path)
    with pytest.raises(ValueError,match='必须是数值单元格'):
        verifier.load_export(path,headers,sheet='结果')
    sheet.cell(2,amount_column).value=True;book.save(path)
    with pytest.raises(ValueError,match='必须是数值单元格'):
        verifier.load_export(path,headers,sheet='结果')


def test_excel_numeric_formula_requires_an_actual_numeric_cache(verifier,gold,tmp_path):
    import zipfile
    from openpyxl import Workbook
    expected=gold['baseline_sep'];headers=list(expected[0]);path=tmp_path/'result.xlsx'
    book=Workbook();sheet=book.active;sheet.title='结果';sheet.append(headers)
    for row in expected:
        sheet.append([row[name] if name in verifier.KEYS else float(Decimal(row[name])) for name in headers])
    amount_column=headers.index('收入元')+1
    sheet.cell(2,amount_column).value='=1+1';book.save(path)
    with pytest.raises(ValueError,match='公式缓存缺失'):
        verifier.load_export(path,headers,sheet='结果')
    # A cached numeric formula is permitted; the independent oracle compares
    # the cached value but does not claim the workbook was recalculated.
    with zipfile.ZipFile(path) as archive:contents={n:archive.read(n) for n in archive.namelist()}
    xml=contents['xl/worksheets/sheet1.xml']
    assert b'<f>1+1</f><v></v>' in xml
    xml=xml.replace(b'<f>1+1</f><v></v>',b'<f>1+1</f><v>'+expected[0]['收入元'].encode()+b'</v>',1)
    contents['xl/worksheets/sheet1.xml']=xml
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in contents.items():archive.writestr(name,data)
    assert verifier.compare(verifier.load_export(path,headers,sheet='结果'),expected)==[]


def test_excel_malformed_or_duplicate_cell_coordinates_are_rejected(verifier,tmp_path):
    import zipfile
    from openpyxl import Workbook
    path=tmp_path/'coordinates.xlsx';book=Workbook();sheet=book.active
    sheet['A1']='one';sheet['B1']='two';book.save(path)
    with zipfile.ZipFile(path) as archive:original={n:archive.read(n) for n in archive.namelist()}
    for source,target,message in [(b'r="B1"',b'r="XFE1"','坐标无效'),
                                  (b'r="B1"',b'r="A1"','重复单元格')]:
        contents=dict(original);xml=contents['xl/worksheets/sheet1.xml']
        assert source in xml
        contents['xl/worksheets/sheet1.xml']=xml.replace(source,target,1)
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,data in contents.items():archive.writestr(name,data)
        with pytest.raises(ValueError,match=message):verifier._xlsx_rows(path,None)


def test_cli_works_without_project_imports(tmp_path):
    result=subprocess.run([sys.executable,'-I','-X','utf8',str(KIT/'verify_export.py'),
                           str(KIT/'expected'/'baseline_sep.csv'),'--scenario','baseline_sep'],
                          text=True,encoding='utf-8',capture_output=True,cwd=tmp_path,timeout=10)
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)['passed'] is True


def test_expected_answers_are_not_in_input_tree():
    inputs=list((KIT/'inputs').rglob('*'))
    assert not any('gold' in p.name.lower() or 'baseline' in p.name.lower() for p in inputs)
    with (KIT/'验收记录模板.csv').open(encoding='utf-8-sig',newline='') as stream:
        rows=list(csv.DictReader(stream))
    assert len(rows)==18
    assert all(r['状态（待执行/通过/失败/阻断）']=='待执行' for r in rows)
