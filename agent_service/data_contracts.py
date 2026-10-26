"""Deterministic release gates. Contracts are code, not executable SQL."""
import hashlib
import json
import math
import re
from decimal import Decimal


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 128 or '\x00' in value:
        raise ValueError('Invalid column name')
    return '"' + value.replace('"', '""') + '"'


def validate(contract):
    if not isinstance(contract, dict) or set(contract) - {'version', 'min_rows', 'max_rows', 'required', 'unique', 'not_null', 'ranges', 'totals'}:
        raise ValueError('Unknown data contract fields')
    if type(contract.get('version')) is not int or contract['version'] != 1:
        raise ValueError('Data contract version must be 1')
    for key in ('min_rows', 'max_rows'):
        if key in contract and (type(contract[key]) is not int or contract[key] < 0):
            raise ValueError('Invalid row count bound')
    if contract.get('max_rows', float('inf')) < contract.get('min_rows', 0):
        raise ValueError('Conflicting row count bounds')
    for key in ('required', 'not_null'):
        fields = contract.get(key, [])
        if not isinstance(fields, list) or len(fields) > 100:
            raise ValueError('Invalid field list')
        for field in fields:
            identifier(field)
    groups = contract.get('unique', [])
    if not isinstance(groups, list) or len(groups) > 20:
        raise ValueError('Invalid unique constraints')
    for group in groups:
        if not isinstance(group, list) or not 1 <= len(group) <= 20:
            raise ValueError('Unique key must contain columns')
        for field in group:
            identifier(field)
    ranges = contract.get('ranges', {})
    if not isinstance(ranges, dict) or len(ranges) > 100:
        raise ValueError('Invalid ranges')
    for field, bounds in ranges.items():
        identifier(field)
        if not isinstance(bounds, dict) or not bounds or set(bounds) - {'min', 'max'}:
            raise ValueError('Invalid range bounds')
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in bounds.values()):
            raise ValueError('Range bounds must be finite numbers')
        if bounds.get('min', -float('inf')) > bounds.get('max', float('inf')):
            raise ValueError('Conflicting range bounds')
    totals=contract.get('totals',{})
    if not isinstance(totals,dict) or len(totals)>20:raise ValueError('Invalid totals')
    for field,bounds in totals.items():
        identifier(field)
        if not isinstance(bounds,dict) or set(bounds)-{'expected','tolerance'} or 'expected' not in bounds:raise ValueError('Invalid total bounds')
        for key,value in bounds.items():
            if not isinstance(value,str) or not re.fullmatch(r'-?\d{1,26}(?:\.\d{1,12})?',value):raise ValueError('Exact amounts must be DECIMAL strings')
        if Decimal(bounds.get('tolerance','0'))<0:raise ValueError('Tolerance cannot be negative')
    return contract


def evaluate(connection, contract):
    """Evaluate only the fixed business output table; never return raw records."""
    validate(contract)
    table = 'business.business_objects'
    columns = {r[0] for r in connection.execute('DESCRIBE ' + table).fetchall()}
    count = connection.execute('SELECT count(*) FROM ' + table).fetchone()[0]
    results = []
    def check(name, passed, failures=0):
        results.append({'name': name, 'status': 'pass' if passed else 'fail', 'failures': int(failures)})
    row_ok=contract.get('min_rows', 0) <= count <= contract.get('max_rows', count)
    check('row_count', row_ok, 0 if row_ok else 1)
    needed = set(contract.get('required', [])) | set(contract.get('not_null', [])) | set(contract.get('ranges', {})) | set(contract.get('totals', {}))
    needed.update(field for group in contract.get('unique', []) for field in group)
    for field in sorted(needed - columns):
        check('missing_column:' + field, False, 1)
    for field in contract.get('not_null', []):
        if field in columns:
            n = connection.execute('SELECT count(*) FROM ' + table + ' WHERE ' + identifier(field) + ' IS NULL').fetchone()[0]
            check('not_null:' + field, n == 0, n)
    for group in contract.get('unique', []):
        if set(group) <= columns:
            fields = ','.join(map(identifier, group))
            n = connection.execute('SELECT coalesce(sum(n-1),0) FROM (SELECT count(*) n FROM ' + table + ' GROUP BY ' + fields + ' HAVING count(*)>1)').fetchone()[0]
            check('unique:' + ','.join(group), n == 0, n)
    for field, bounds in contract.get('ranges', {}).items():
        if field not in columns:
            continue
        col = identifier(field)
        predicates = [col + ' IS NOT NULL AND (TRY_CAST(' + col + ' AS DOUBLE) IS NULL OR NOT isfinite(TRY_CAST(' + col + ' AS DOUBLE)))']
        parameters = []
        for side, operator in (('min', '<'), ('max', '>')):
            if side in bounds:
                predicates.append('TRY_CAST(' + col + ' AS DOUBLE)' + operator + '?')
                parameters.append(bounds[side])
        n = connection.execute('SELECT count(*) FROM ' + table + ' WHERE ' + ' OR '.join('(' + p + ')' for p in predicates), parameters).fetchone()[0]
        check('range:' + field, n == 0, n)
    for field,bounds in contract.get('totals',{}).items():
        if field not in columns:continue
        col=identifier(field)
        value='TRY_CAST('+col+' AS DECIMAL(38,12))'
        invalid=connection.execute('SELECT count(*) FROM '+table+' WHERE '+col+' IS NULL OR '+value+" IS NULL OR NOT regexp_full_match(trim(CAST("+col+" AS VARCHAR)), '-?[0-9]{1,26}(\\.[0-9]{1,12})?')").fetchone()[0]
        total=connection.execute('SELECT coalesce(sum('+value+'),0) FROM '+table).fetchone()[0]
        expected=Decimal(bounds['expected']);tolerance=Decimal(bounds.get('tolerance','0'))
        # Decimal's default context is too small for 38-digit exact accounting.
        from decimal import localcontext
        with localcontext() as ctx:
            ctx.prec=80
            passed=invalid==0 and abs(total-expected)<=tolerance
        check('total:'+field,passed,invalid if invalid else 0 if passed else 1)
    encoded = json.dumps(contract, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return {'version': 1, 'contract_sha256': hashlib.sha256(encoded).hexdigest(), 'passed': all(r['status'] == 'pass' for r in results), 'rows': count, 'checks': results}


def select_contract(project_id,application_id):
    """Read an application override or the project default. No implicit merge."""
    from .workspace import path_for
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',application_id):raise ValueError('Invalid application id')
    for name in ('contracts/'+application_id+'.json','data-contract.json'):
        path=path_for(project_id,name)
        if not path.exists():continue
        if path.stat().st_size>65536:raise ValueError('数据契约文件过大')
        return validate(json.loads(path.read_text()))
    return None
