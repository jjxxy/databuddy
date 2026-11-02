"""Create a synthetic presentation for layout review, not AI acceptance."""
import argparse
from decimal import Decimal
from pathlib import Path

from agent_service.office import Spec
from agent_service.office_presentation import build, verify
from pptx import Presentation


def make_sample(path):
    data=[('东区','1248600.00','34200.00'),('西区','960000.00','23400.00'),
          ('南区','850300.00','15250.00'),('北区','745000.00','14900.00'),
          ('中区','682000.00','12000.00'),('线上','1085000.00','29000.00'),('直营','450000.00','9200.00')]
    rows=[[name,f'{Decimal(revenue):,.2f}',f'{Decimal(refund):,.2f}',f'{Decimal(revenue)-Decimal(refund):,.2f}'] for name,revenue,refund in data]
    revenue=sum((Decimal(row[1]) for row in data),Decimal(0));refund=sum((Decimal(row[2]) for row in data),Decimal(0))
    rows.append(['合计',f'{revenue:,.2f}',f'{refund:,.2f}',f'{revenue-refund:,.2f}'])
    spec=Spec.model_validate({'kind':'presentation','title':'2026 年度经营分析与财务月结','sections':[
        {'heading':'经营情况与核对范围','bullets':['月度结账范围覆盖销售收入、退款及应收账款。','净收入和退款金额均依据已核对的订单数据。','本月结果不包含尚未确认的调整事项。']},
        {'heading':'分区域收入明细','table':{'columns':['区域','收入（元）','退款（元）','净额（元）'],'rows':rows}},
        {'heading':'区域净收入比较','chart':{'title':'本月各区域净收入','unit':'元','points':[
            {'label':name,'value':str(Decimal(revenue)-Decimal(refund))} for name,revenue,refund in data[:3]]}}]})
    build(spec,path);verify(spec,Presentation(path));return len(Presentation(path).slides)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);args=parser.parse_args()
    print(f'Synthetic quality sample: {make_sample(args.output)} slides; all source values structurally verified')
