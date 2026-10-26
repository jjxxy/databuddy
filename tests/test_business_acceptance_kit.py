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


@pytest.mark.parametrize('bad',['NaN','Infinity','460001.31','460,001.30','¥460001.30'])
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
