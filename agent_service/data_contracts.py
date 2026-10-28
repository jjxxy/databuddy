"""Deterministic release gates. Contracts are code, not executable SQL."""
import hashlib
import json
import os
import re
from decimal import Decimal
from pathlib import Path

_EXACT_AMOUNT = r'-?[0-9]{1,26}(?:\.[0-9]{1,12})?'
_JS_SAFE_INTEGER = 2**53 - 1


class ExactDataContractError(ValueError):
    """Controlled, user-actionable numeric precision or evidence failure."""


class NumericLineageError(ExactDataContractError):
    """Unsafe or unverifiable historical numeric lineage; never includes raw data."""
    def __init__(self, reason, message):
        self.reason = reason
        super().__init__(message)


def _range_bound(value):
    """Keep integer thresholds compatible; decimal thresholds are fixed-point text.

    Floating-point JSON values have already lost their original decimal token.
    Large JSON integers can also be rounded by a browser before reaching us.
    Both must be supplied as decimal strings instead of guessed back to exactness.
    """
    if type(value) is int and abs(value) <= _JS_SAFE_INTEGER:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(_EXACT_AMOUNT, value):
        raise ExactDataContractError('范围阈值须使用安全整数或最多26位整数、12位小数的十进制字符串；不接受浮点数、科学计数法或超出范围的值')
    return Decimal(value)


def _exact_column_type(field, kind):
    """Do not relabel an already rounded floating value as an exact decimal."""
    integers = {'TINYINT', 'SMALLINT', 'INTEGER', 'BIGINT', 'HUGEINT',
                'UTINYINT', 'USMALLINT', 'UINTEGER', 'UBIGINT', 'UHUGEINT'}
    decimal = re.fullmatch(r'DECIMAL\((\d+),(\d+)\)', kind)
    if kind in integers or kind == 'VARCHAR' or decimal and int(decimal.group(2)) <= 12:
        return
    raise ExactDataContractError('字段“'+' '.join(field.split())[:120]+'”的类型不支持精确金额校验；请从原始资料转换为整数、固定小数或十进制文本后重新运行')


def _reject_input_view(connection):
    """A view can disguise a lossy DOUBLE→DECIMAL cast as a decimal column."""
    views = connection.execute("SELECT sql FROM duckdb_views() WHERE database_name=current_database() "
                               "AND schema_name='business' AND view_name='business_objects'").fetchall()
    if views:
        raise ExactDataContractError('数据验收不能对未经核对的视图转换标记精确；请从原始资料重新运行可追溯的处理流程')


def _contract_digest(contract):
    encoded = json.dumps(contract, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def require_exact_numeric_evidence(report, contract=None, *, expected_rows=None):
    """Do not reuse historical pass receipts made with floating-point checks."""
    checks = report.get('checks') if isinstance(report, dict) else None
    numeric_names = {str(item.get('name')) for item in checks if isinstance(item, dict)
                     and isinstance(item.get('name'), str)
                     and item['name'].startswith(('range:', 'total:'))} if isinstance(checks,list) else set()
    expected = set()
    if isinstance(contract, dict):
        for prefix, key in (('range:', 'ranges'), ('total:', 'totals')):
            if isinstance(contract.get(key), dict):
                expected.update(prefix + field for field in contract[key] if isinstance(field, str))
    if not numeric_names and not expected:
        return
    message = '历史数值范围或对账检查的精度尚未确认，请从原始资料重新运行并核对后再采用'
    if (not isinstance(report, dict) or report.get('numeric_policy') != 'exact_decimal_v1'
            or report.get('passed') is not True or not isinstance(checks, list)):
        raise ExactDataContractError(message)
    if (type(expected_rows) is not int or expected_rows < 0 or type(report.get('rows')) is not int
            or report['rows'] != expected_rows):
        raise ExactDataContractError(message)
    if not isinstance(contract, dict) or report.get('contract_sha256') != _contract_digest(contract):
        raise ExactDataContractError(message)
    if numeric_names != expected:
        raise ExactDataContractError(message)
    for name in expected:
        items = [item for item in checks if isinstance(item, dict) and item.get('name') == name]
        if (len(items) != 1 or items[0].get('status') != 'pass'
                or type(items[0].get('failures')) is not int or items[0]['failures'] != 0):
            raise ExactDataContractError(message)


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
        decoded = {side: _range_bound(value) for side, value in bounds.items()}
        if 'min' in decoded and 'max' in decoded and decoded['min'] > decoded['max']:
            raise ExactDataContractError('数值范围下限不能高于上限，请核对后重新运行')
    totals=contract.get('totals',{})
    if not isinstance(totals,dict) or len(totals)>20:raise ValueError('Invalid totals')
    for field,bounds in totals.items():
        identifier(field)
        if not isinstance(bounds,dict) or set(bounds)-{'expected','tolerance'} or 'expected' not in bounds:raise ValueError('Invalid total bounds')
        for key,value in bounds.items():
            if not isinstance(value,str) or not re.fullmatch(_EXACT_AMOUNT,value):raise ExactDataContractError('对账金额和容差须使用最多26位整数、12位小数的固定十进制字符串')
        if Decimal(bounds.get('tolerance','0'))<0:raise ExactDataContractError('对账容差不能为负数')
    return contract


def _evaluate(connection, contract, *, verified_parquet=False):
    """Evaluate only the fixed business output table; never return raw records."""
    validate(contract)
    if (contract.get('ranges') or contract.get('totals')) and not verified_parquet:
        _reject_input_view(connection)
    table = 'business.business_objects'
    column_types = {row[0]: str(row[1]).upper() for row in connection.execute('DESCRIBE ' + table).fetchall()}
    columns = set(column_types)
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
        _exact_column_type(field, column_types[field])
        col = identifier(field)
        value = 'TRY_CAST(' + col + ' AS DECIMAL(38,12))'
        valid_text = "regexp_full_match(trim(CAST("+col+" AS VARCHAR)), '"+_EXACT_AMOUNT+"')"
        predicates = [col + ' IS NOT NULL AND (' + value + ' IS NULL OR NOT ' + valid_text + ')']
        parameters = []
        for side, operator in (('min', '<'), ('max', '>')):
            if side in bounds:
                predicates.append(value + operator + 'CAST(? AS DECIMAL(38,12))')
                parameters.append(format(_range_bound(bounds[side]), 'f'))
        n = connection.execute('SELECT count(*) FROM ' + table + ' WHERE ' + ' OR '.join('(' + p + ')' for p in predicates), parameters).fetchone()[0]
        check('range:' + field, n == 0, n)
    for field,bounds in contract.get('totals',{}).items():
        if field not in columns:continue
        _exact_column_type(field, column_types[field])
        col=identifier(field)
        value='TRY_CAST('+col+' AS DECIMAL(38,12))'
        invalid=connection.execute('SELECT count(*) FROM '+table+' WHERE '+col+' IS NULL OR '+value+" IS NULL OR NOT regexp_full_match(trim(CAST("+col+" AS VARCHAR)), '"+_EXACT_AMOUNT+"')").fetchone()[0]
        total=connection.execute('SELECT coalesce(sum('+value+'),0) FROM '+table).fetchone()[0]
        expected=Decimal(bounds['expected']);tolerance=Decimal(bounds.get('tolerance','0'))
        # Decimal's default context is too small for 38-digit exact accounting.
        from decimal import localcontext
        with localcontext() as ctx:
            ctx.prec=80
            passed=invalid==0 and abs(total-expected)<=tolerance
        check('total:'+field,passed,invalid if invalid else 0 if passed else 1)
    return {'version': 1, 'numeric_policy': 'exact_decimal_v1', 'contract_sha256': _contract_digest(contract), 'passed': all(r['status'] == 'pass' for r in results), 'rows': count, 'checks': results}


def evaluate(connection, contract):
    return _evaluate(connection, contract)


def evaluate_verified_parquet(contract, path, expected_sha256):
    """Evaluate the release's actual hash-bound Parquet, not a supplied view."""
    import duckdb
    from .platform_files import open_regular_read
    path = Path(path)
    def checksum():
        digest = hashlib.sha256()
        with os.fdopen(open_regular_read(path),'rb') as stream:
            while block := stream.read(1024*1024):digest.update(block)
        return digest.hexdigest()
    if not isinstance(expected_sha256, str) or checksum() != expected_sha256:
        raise ValueError('结果文件完整性校验失败，禁止采用')
    with duckdb.connect() as connection:
        connection.execute('CREATE SCHEMA business')
        connection.read_parquet(str(path)).create_view('contract_input')
        connection.execute('CREATE VIEW business.business_objects AS SELECT * FROM contract_input')
        result = _evaluate(connection, contract, verified_parquet=True)
    if checksum() != expected_sha256:
        raise ValueError('结果文件在校验期间发生变化，禁止采用')
    return result


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
