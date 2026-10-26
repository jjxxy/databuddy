from decimal import Decimal,localcontext
import pytest
from agent_service.evidence_tables import parse_html_tables,parse_indented_table,validate_facts,number


def test_html_multi_level_headers_keep_each_value_and_source_coordinate():
    html='<script>ignore(999)</script><table><caption>2026-10 收入（万元）</caption><tr><th rowspan="2">办事处</th><th colspan="2">收入</th></tr><tr><th>本月</th><th>上月</th></tr><tr><td>上海</td><td>15.12</td><td>11.34</td></tr></table>'
    table=parse_html_tables(html,source_id='s')[0]
    assert [f['header_path'] for f in table['fields']]==[['办事处'],['收入','本月'],['收入','上月']]
    assert table['rows']==[{'c_1':'上海','c_2':'15.12','c_3':'11.34'}]
    assert table['provenance'][1]['html_column']==2 and table['provenance'][1]['html_row']==3
    assert table['semantic_review_required'] and not table['context_verified']
    assert '999' not in str(table)


def test_html_detail_merges_never_replicate_measures():
    table=parse_html_tables('<table><tr><th>A</th><th>B</th></tr><tr><td rowspan="2">10</td><td>x</td></tr><tr><td>y</td></tr></table>')[0]
    assert table['rows'][1]['c_1'] is None
    assert not table['complete'] and table['unresolved']


def test_html_blank_rows_do_not_shift_provenance():
    t=parse_html_tables('<table><tr><th>A</th></tr><tr><td></td></tr><tr><td>x</td></tr></table>')[0]
    assert t['rows']==[{'c_1':'x'}]
    assert len(t['provenance'])==1 and t['provenance'][0]['row_index']==1 and t['provenance'][0]['html_row']==3


@pytest.mark.parametrize('html',['<table><tr><td>x','<table><tr><td colspan="0">x</td></tr></table>','<table><tr><td><table></table></td></tr></table>'])
def test_html_refuses_incomplete_or_ambiguous_structure(html):
    with pytest.raises(ValueError):parse_html_tables(html)


def test_indentation_keeps_all_rows_and_layout_context():
    t=parse_indented_table('华东\n  上海  12  15%\n  安徽  8  10%\n华北\n  北京  7  9%', ['办事处','收入','增长率'])
    assert len(t['rows'])==3 and t['rows'][0]['_hierarchy']==['华东','上海']
    assert t['provenance'][-1]['line']==5
    with pytest.raises(ValueError):parse_indented_table('上海  12  15%  意外列',['办事处','收入','增长率'])


def test_whole_numeric_token_and_long_percent_are_exact():
    text='上海2026-10收入1123元'
    fact={'value_as_printed':'123','measure':'收入','unit':'元','period':'2026-10','dimensions':{'office':'上海'},'start':0,'end':len(text),'quote':text}
    with pytest.raises(ValueError):validate_facts(text,[fact])
    fact['value_as_printed']='1123'
    assert validate_facts(text,[fact])[0]['normalized_value']=='1123'
    printed='123456789012345678901234567890.123456789012%'
    with localcontext() as ctx:
        ctx.prec=100
        assert number(printed)==Decimal(printed[:-1])/100
def test_ambiguous_comma_layout_cannot_silently_change_a_fact_amount():
    from decimal import Decimal
    from agent_service.evidence_tables import number
    assert number('1,234.567')==Decimal('1234.567')
    assert number('1，234.5％')==Decimal('12.345')
    assert number('1,23') is None and number('1,2,3') is None

