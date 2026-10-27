"""Business rules compile from typed configuration, never interpolated SQL expressions."""
from datetime import date
from decimal import Decimal,InvalidOperation,localcontext

NUMERIC_POLICY='decimal_fixedpoint_v1'
LEGACY_DIVIDE_MESSAGE=('此旧式加工入口的除法会经过二进制浮点，当前已停用，未执行；'
                       '请改用数据工作流的精确Decimal compute，并明确舍入位数')

def fixed_filter_number(value):
    if isinstance(value,(float,bool)):
        raise ValueError('精确数值筛选须使用整数或十进制字符串（例如"0.13"），不能把二进制浮点转换成精确阈值')
    text=str(value)
    if len(text)>2000:raise ValueError('数值筛选值过长')
    try:n=Decimal(text)
    except InvalidOperation:raise ValueError('数值筛选值无效') from None
    # Compare without context-sensitive abs()/formatting of a huge exponent.
    if not n.is_finite() or n.copy_abs()>Decimal('1e15'):raise ValueError('数值筛选值超出范围')
    if n.is_zero():return '0'
    if n.adjusted() < -12:raise ValueError('数值筛选值须能精确表示为最多12位小数，未自动舍入')
    with localcontext() as context:
        context.prec=100
        rounded=n.quantize(Decimal('0.000000000001'))
    if rounded!=n:raise ValueError('数值筛选值须能精确表示为最多12位小数，未自动舍入')
    return format(rounded,'f')

def validate_legacy_numeric(spec,*,current_policy=True):
    if any(rule.get('operation')=='divide' for rule in spec.get('computations',[])):
        raise ValueError(LEGACY_DIVIDE_MESSAGE)
    for rule in spec.get('filters',[]):
        if rule.get('data_type')!='number' or rule.get('operator') in ('is_null','not_null'):continue
        value=rule.get('value');values=value if isinstance(value,list) else [value]
        for number in values:
            fixed_filter_number(number)
            if not current_policy and 'e' in str(Decimal(str(number))).lower():
                raise ValueError('旧冻结方案的科学计数筛选可能丢失精度，未执行；请重新生成精确候选并核对采用')

def literal(value):
    return "'"+str(value).replace("'","''")+"'"

def numeric(field):
    return 'cast(replace(cast("'+field+'" as varchar),\',\',\'\') as decimal(38,12))'

def filter_sql(rule):
    field='"'+rule['field']+'"';kind=rule['data_type'];op=rule['operator'];value=rule.get('value')
    if op in ('is_null','not_null'):return field+(' is null' if op=='is_null' else ' is not null')
    values=value if op in ('in','not_in') else [value]
    if kind=='number':
        encoded=[]
        for v in values:
            encoded.append(fixed_filter_number(v))
        field=numeric(rule['field'])
    elif kind=='date':
        encoded=['date '+literal(date.fromisoformat(str(v)).isoformat()) for v in values]
        field='cast('+field+' as date)'
    else:
        encoded=[literal(v) for v in values];field='cast('+field+' as varchar)'
    if op in ('in','not_in'):return field+(' in (' if op=='in' else ' not in (')+','.join(encoded)+')'
    if op=='contains':
        if kind!='text':raise ValueError('包含筛选只能用于文本')
        return 'contains('+field+','+encoded[0]+')'
    operators={'eq':'=','neq':'<>','gt':'>','gte':'>=','lt':'<','lte':'<='}
    return field+' '+operators[op]+' '+encoded[0]

def computation_sql(rule):
    if rule['operation']=='divide':raise ValueError(LEGACY_DIVIDE_MESSAGE)
    left=numeric(rule['left_field']);right=numeric(rule['right_field'])
    operators={'add':'+','subtract':'-','multiply':'*'}
    return '('+left+' '+operators[rule['operation']]+' '+right+')'
