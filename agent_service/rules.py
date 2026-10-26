"""Business rules compile from typed configuration, never interpolated SQL expressions."""
from datetime import date
from decimal import Decimal,InvalidOperation

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
            try:n=Decimal(str(v))
            except InvalidOperation:raise ValueError('数值筛选值无效')
            if not n.is_finite() or abs(n)>Decimal('1e15'):raise ValueError('数值筛选值超出范围')
            encoded.append(str(n))
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
    left=numeric(rule['left_field']);right=numeric(rule['right_field'])
    operators={'add':'+','subtract':'-','multiply':'*','divide':'/'}
    return '('+left+' '+operators[rule['operation']]+' '+right+')'
