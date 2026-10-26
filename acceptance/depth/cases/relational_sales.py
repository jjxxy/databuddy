"""Cross-file sales reconciliation with multiple refund rows per order."""
from __future__ import annotations

from collections import defaultdict
import csv
from decimal import Decimal
import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('depth_finance_validation',Path(__file__).with_name('large_finance.py'))
shared=importlib.util.module_from_spec(spec);spec.loader.exec_module(shared)
KEYS=('office',)
VALUES=('paid_orders','gross_amount','refund_amount','net_amount')


def fixture(directory):
    directory.mkdir(parents=True,exist_ok=True)
    customers=directory/'sales_customers_500.csv'
    orders=directory/'sales_orders_5000.csv'
    refunds=directory/'sales_refunds_3750.csv'
    with customers.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['customer_id','office','customer_name'])
        for i in range(1,501):writer.writerow([f'C{i:04d}',shared.OFFICES[i%5],f'合成客户{i:04d}'])
    with orders.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['order_id','customer_id','order_month','payment_status','gross_amount_text'])
        for i in range(1,5001):
            amount=Decimal((i*173)%999999+10000)/100+Decimal(i%991)/Decimal('1000000000000')
            writer.writerow([f'O{i:05d}',f'C{(i*37)%500+1:04d}','2026-08' if i%3==0 else '2026-09',
                'paid' if i%7 not in (0,1) else 'pending' if i%7==0 else 'cancelled',f'{amount:,.12f}'])
    with refunds.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(['refund_id','order_id','refund_status','refund_amount_text'])
        for i in range(4,5001,4):
            for j in range(3):
                amount=Decimal((i*11+j*7)%1900+1)/100+Decimal((i+j)%31)/Decimal('1000000000000')
                status='success' if j==0 or (j==1 and i%3) else 'pending' if j==1 else 'failed'
                writer.writerow([f'R{i:05d}-{j}',f'O{i:05d}',status,f'{amount:,.12f}'])
    # Calculate from independently loaded input tables; one order contributes
    # gross once, while successful refunds are first summed at order grain.
    with customers.open(encoding='utf-8-sig',newline='') as stream:
        customer_rows=list(csv.DictReader(stream));office={r['customer_id']:r['office'] for r in customer_rows}
    assert len(customer_rows)==len(office)==500
    refund_by_order=defaultdict(Decimal);refund_rows=0;per_order=defaultdict(int)
    with refunds.open(encoding='utf-8-sig',newline='') as stream:
        for row in csv.DictReader(stream):
            refund_rows+=1;per_order[row['order_id']]+=1
            if row['refund_status']=='success':refund_by_order[row['order_id']]+=shared.money(row['refund_amount_text'])
    golden=defaultdict(lambda:{name:Decimal(0) for name in VALUES});order_rows=0;selected_orders=0
    with orders.open(encoding='utf-8-sig',newline='') as stream:
        for row in csv.DictReader(stream):
            order_rows+=1
            if row['payment_status']!='paid' or row['order_month']!='2026-09':continue
            selected_orders+=1;value=golden[(office[row['customer_id']],)]
            gross=shared.money(row['gross_amount_text']);refund=refund_by_order[row['order_id']]
            value['paid_orders']+=1;value['gross_amount']+=gross
            value['refund_amount']+=refund;value['net_amount']+=gross-refund
    assert order_rows==5000 and refund_rows==3750 and max(per_order.values())==3 and len(golden)==5
    return [orders,customers,refunds],dict(golden),{'order_rows':order_rows,'customer_rows':len(customer_rows),
        'refund_rows':refund_rows,'multiple_refunds_per_order':True,'selected_order_rows':selected_orders,
        'expected_groups':len(golden)}


def run(ctx):
    paths,golden,counts=fixture(ctx.evidence/'inputs')
    prompt=('请用本项目三份附件 sales_orders_5000.csv、sales_customers_500.csv、sales_refunds_3750.csv '
        '完成2026年9月已付款销售净收入对账。使用全部资料，预览不能当全量。'
        '订单仅选 order_month=2026-09 且 payment_status=paid；按 customer_id 关联客户归属办事处 office。'
        '每个订单的订单金额只计一次。退款表同一订单可以多笔，仅扣 refund_status=success 的全部退款；'
        '不按退款发生月额外筛选，pending/failed退款不扣，无成功退款的订单按0扣减但必须保留。'
        '不要让一对多退款关联放大订单金额或订单数。gross_amount_text 和 refund_amount_text 为人民币元，'
        '英文逗号仅为千位分隔符；用精确十进制保留原有12位小数，净收入=订单金额-成功退款。'
        '交付可下载的完整CSV办事处汇总或可复用数据应用及完整结果，列名固定为 '
        'office,paid_orders,gross_amount,refund_amount,net_amount。不要加总计行、不要四舍五入。'
        '另保存说明，列出三份来源文件名、单位、筛选、关联粒度与多笔退款处理方式。'
        '数据应用/工作流保持候选供我人工核对，并给完整候选材料；不要替我发布、采用或确认口径。')
    return shared.execute(ctx,paths,golden,counts,prompt,KEYS,VALUES)
