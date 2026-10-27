"""Typed, reproducible table workflows. Plans are data, never executable Python.

Every stage operates on complete bounded tables. Exceeding a bound fails the
workflow rather than returning a truncated result that looks complete.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import date,datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Annotated, Any, Callable, Literal

import duckdb
import sqlglot
from pydantic import Field, model_validator
from sqlglot import exp

from .models import StrictModel
from .workbook_reader import WorkbookReadError, parse_localized_value

NAME = r'^[A-Za-z][A-Za-z0-9_]{0,63}$'
Scalar = str | int | bool | Decimal | None


class WorkflowError(ValueError):
    def __init__(self, code: str, message: str, stage_id: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.code, self.stage_id = code, stage_id
        # Only server-owned coordinates/types may leave the processing boundary.
        # Never copy raw cell values, arbitrary keys, or custom Python type names.
        self.details={}
        if isinstance(details,dict):
            column=details.get('column');row=details.get('row_index')
            expected=details.get('expected_data_type');actual=details.get('actual_type')
            if isinstance(column,str) and re.fullmatch(NAME,column):self.details['column']=column
            if type(row) is int and row>=1:self.details['row_index']=row
            if isinstance(expected,str) and expected in {'text','decimal','integer','boolean','date'}:
                self.details['expected_data_type']=expected
            if isinstance(actual,str) and actual in {'NoneType','str','int','float','bool','Decimal','date','datetime','list','dict','bytes','other'}:
                self.details['actual_type']=actual


def _actual_type(value):
    return {type(None):'NoneType',str:'str',int:'int',float:'float',bool:'bool',Decimal:'Decimal',
            date:'date',datetime:'datetime',list:'list',dict:'dict',bytes:'bytes'}.get(type(value),'other')


class Column(StrictModel):
    model_config={'json_schema_extra':{'allOf':[{
        'if':{'properties':{'data_type':{'enum':['decimal','integer']}},'required':['data_type']},
        'then':{'required':['unit'],'properties':{'unit':{'minLength':1}}}}]}}
    name: str = Field(pattern=NAME,description='Stable ASCII identifier; use label for the original Chinese display name.')
    data_type: Literal['text', 'decimal', 'integer', 'boolean', 'date']
    meaning: str = Field(min_length=1, max_length=1000)
    label: str = Field(default='', max_length=200)
    value_format: Literal['native', 'percent_fraction', 'percent_points'] = 'native'
    number_format: str = Field(default='', max_length=255)
    unit: str = Field(default='', max_length=80,description='Required for decimal/integer: e.g. CNY for amounts or 1 for counts. Units and exchange rates are never inferred.')
    period: str = Field(default='', max_length=120)
    aggregation: Literal['additive', 'non_additive', 'unknown'] = 'unknown'
    nullable: bool = True

    @model_validator(mode='after')
    def numeric_unit(self):
        if self.data_type in ('decimal', 'integer') and not self.unit:
            raise ValueError('Numeric columns require an explicit unit, such as CNY or 1')
        if self.value_format != 'native' and self.data_type not in ('decimal', 'integer'):
            raise ValueError('Percentage value formats require a numeric column')
        return self


class TableSchema(StrictModel):
    columns: list[Column] = Field(min_length=1, max_length=200)
    grain: list[str] = Field(max_length=20)
    meaning: str = Field(min_length=1, max_length=1000)
    source_ref: str = Field(default='', max_length=400)

    @model_validator(mode='after')
    def valid_schema(self):
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names) or len(set(self.grain)) != len(self.grain):
            raise ValueError('Duplicate column or grain identifier')
        if not set(self.grain) <= set(names):
            raise ValueError('Grain references a missing column')
        return self


class Condition(StrictModel):
    column: str = Field(pattern=NAME)
    operator: Literal['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'in', 'not_in', 'contains', 'is_null', 'not_null']
    value: str | int | bool | list[str | int | bool] | None = None

    @model_validator(mode='after')
    def valid_operand(self):
        if self.operator not in ('is_null', 'not_null'):
            if self.value is None:
                raise ValueError('Condition requires a value')
            if (self.operator in ('in', 'not_in')) != isinstance(self.value, list):
                raise ValueError('Set conditions require a list; other conditions require one value')
        return self


class Stage(StrictModel):
    id: str = Field(pattern=NAME)
    description: str = Field(min_length=1, max_length=1000)


class Select(Stage):
    kind: Literal['select']
    input: str = Field(pattern=NAME)
    columns: list[str] = Field(min_length=1, max_length=200)


class Drop(Stage):
    kind: Literal['drop']
    input: str = Field(pattern=NAME)
    columns: list[str] = Field(min_length=1, max_length=199)


class Filter(Stage):
    kind: Literal['filter']
    input: str = Field(pattern=NAME)
    conditions: list[Condition] = Field(min_length=1, max_length=50)


class Reclassify(Stage):
    kind: Literal['reclassify']
    input: str = Field(pattern=NAME)
    column: str = Field(pattern=NAME)
    output: Column
    mapping: dict[str, str] = Field(min_length=1, max_length=10000)
    unmapped: Literal['error', 'preserve'] = 'error'


class Calculation(StrictModel):
    output: Column
    left: str = Field(pattern=NAME)
    right: str = Field(pattern=NAME)
    operation: Literal['add', 'subtract', 'multiply', 'divide']
    rounding_scale: int | None = Field(default=None, ge=0, le=12)


class Compute(Stage):
    kind: Literal['compute']
    input: str = Field(pattern=NAME)
    calculations: list[Calculation] = Field(min_length=1, max_length=20)


class Aggregation(StrictModel):
    function: Literal['sum', 'min', 'max', 'count', 'concat']
    column: str | None = Field(default=None, pattern=NAME)
    output: Column
    delimiter: str = Field(default='\n', max_length=20)
    order_by: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode='after')
    def requires_column(self):
        if self.function != 'count' and self.column is None:
            raise ValueError('Aggregation requires a column')
        return self


class Aggregate(Stage):
    kind: Literal['aggregate']
    input: str = Field(pattern=NAME)
    by: list[str] = Field(max_length=20)
    aggregations: list[Aggregation] = Field(min_length=1, max_length=100)
    meaning: str = Field(min_length=1, max_length=1000)


class Lookup(Stage):
    kind: Literal['lookup']
    input: str = Field(pattern=NAME)
    right: str = Field(pattern=NAME)
    left_key: str = Field(pattern=NAME)
    right_key: str = Field(pattern=NAME)
    outputs: dict[str, Column] = Field(min_length=1, max_length=100)
    unmapped: Literal['error', 'null'] = 'error'


class UnionRows(Stage):
    kind: Literal['union_rows']
    inputs: list[str] = Field(min_length=2, max_length=10)


class SQL(Stage):
    kind: Literal['sql']
    inputs: list[str] = Field(min_length=1, max_length=10)
    sql: str = Field(min_length=1, max_length=20000)
    output_schema: TableSchema


class Normalization(StrictModel):
    column: str = Field(pattern=NAME)
    output: Column
    kind: Literal['decimal', 'percent', 'date']
    currency: Literal['CNY', 'USD', 'EUR', 'JPY'] | None = None
    source_unit: str | None = Field(default=None, min_length=1, max_length=80)
    unit_multiplier: str = Field(default='1', pattern=r'^[0-9]{1,26}(?:\.[0-9]{1,12})?$')
    date_format: str | None = Field(default=None, max_length=40)
    missing_policy: Literal['error', 'null'] = 'error'
    missing_tokens: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def explicit_policy(self):
        # Validate policy independently of the row values, including all-null columns.
        try:
            parse_localized_value(None, kind=self.kind, currency=self.currency, unit=self.source_unit,
                                  unit_multiplier=self.unit_multiplier, date_format=self.date_format,
                                  missing_policy='null', missing_tokens=self.missing_tokens)
        except WorkbookReadError as error:
            raise ValueError(str(error)) from error
        multiplier = Decimal(self.unit_multiplier)
        if self.kind == 'date':
            if self.output.data_type != 'date':
                raise ValueError('Date normalization requires a date output column')
        else:
            if self.output.data_type not in ('decimal', 'integer'):
                raise ValueError('Numeric normalization requires a numeric output column')
            if self.kind == 'percent':
                expected = {'percent_fraction': Decimal(1), 'percent_points': Decimal(100)}
                if self.output.unit not in ('%', '1') or expected.get(self.output.value_format) != multiplier:
                    raise ValueError('Percent output requires explicit fraction/points semantics and compatible multiplier')
            elif self.output.value_format != 'native':
                raise ValueError('Plain numeric normalization cannot declare a percentage representation')
            if self.currency:
                compatible = {self.currency, '元'} if self.currency == 'CNY' else {self.currency}
                if self.output.unit not in compatible:
                    raise ValueError('Declared currency must match the output unit; exchange rates are not inferred')
            if self.kind == 'decimal':
                # Check the conversion itself, independently of the optional
                # currency-symbol parsing flag. An omitted flag must not turn
                # a known source denomination into an arbitrary scalar.
                scales = {'元': ('CNY', Decimal(1)), '人民币': ('CNY', Decimal(1)),
                          'CNY': ('CNY', Decimal(1)), '千元': ('CNY', Decimal(1000)),
                          '万元': ('CNY', Decimal(10000)), '亿元': ('CNY', Decimal(100000000)),
                          'USD': ('USD', Decimal(1)), 'EUR': ('EUR', Decimal(1)),
                          'JPY': ('JPY', Decimal(1))}
                if self.source_unit:
                    source, target = scales.get(self.source_unit), scales.get(self.output.unit)
                    if source and target and source[0] == target[0]:
                        if multiplier != source[1] / target[1]:
                            raise ValueError('Declared currency source unit requires its explicit matching base-currency multiplier')
                    elif self.source_unit != self.output.unit or multiplier != 1:
                        raise ValueError('No verified conversion is defined between the declared source and output units')
                elif multiplier != 1:
                    raise ValueError('A numeric scale conversion requires an explicit source unit')
        return self


class Normalize(Stage):
    kind: Literal['normalize']
    input: str = Field(pattern=NAME)
    rules: list[Normalization] = Field(min_length=1, max_length=20)


Operation = Annotated[Select | Drop | Filter | Reclassify | Compute | Aggregate | Lookup | UnionRows | Normalize | SQL,
                      Field(discriminator='kind')]


class Budget(StrictModel):
    max_rows: int = Field(default=100000, ge=1, le=1000000)
    max_cells: int = Field(default=2000000, ge=1, le=10000000)
    max_bytes: int = Field(default=64 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024)
    timeout_seconds: int = Field(default=60, ge=1, le=300)
    sql_timeout_seconds: int = Field(default=10, ge=1, le=30)


class WorkflowPlan(StrictModel):
    version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=120)
    inputs: dict[str, TableSchema] = Field(min_length=1, max_length=10)
    stages: list[Operation] = Field(min_length=1, max_length=30)
    output: str = Field(pattern=NAME)
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode='after')
    def dependency_graph(self):
        known = set(self.inputs)
        if any(not re.fullmatch(NAME, name) for name in known):
            raise ValueError('Invalid input table alias')
        for stage in self.stages:
            if stage.id in known:
                raise ValueError('Duplicate stage or input identifier')
            dependencies = dependencies_for(stage)
            if not set(dependencies) <= known:
                raise ValueError('Stages must reference existing earlier dependencies, not missing or future stages')
            if len(set(dependencies)) != len(dependencies):
                raise ValueError('Duplicate dependency')
            known.add(stage.id)
        if self.output not in {s.id for s in self.stages}:
            raise ValueError('Workflow output must name an executed stage')
        return self


def dependencies_for(stage: Stage) -> list[str]:
    if isinstance(stage, (UnionRows, SQL)):
        return stage.inputs
    if isinstance(stage, Lookup):
        return [stage.input, stage.right]
    return [stage.input]


def _json_value(value):
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError('Unsupported workflow value type')


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      default=_json_value, allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def decimal_value(value) -> Decimal:
    """No binary float coercion and no silent rounding past DECIMAL(38,12)."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise WorkflowError('INVALID_DECIMAL', 'Numeric data must use integers or exact decimal strings')
    if isinstance(value, str) and not re.fullmatch(r'-?\d+(?:\.\d+)?', value):
        raise WorkflowError('INVALID_DECIMAL', 'Numeric data contains an invalid decimal representation')
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise WorkflowError('INVALID_DECIMAL', 'Numeric data contains an invalid decimal representation') from None
    if not number.is_finite():
        raise WorkflowError('INVALID_DECIMAL', 'Non-finite numeric values are not supported')
    with localcontext() as ctx:
        ctx.prec = 100
        if abs(number) >= Decimal('1e26') or number != number.quantize(Decimal('0.000000000001')):
            raise WorkflowError('DECIMAL_PRECISION', 'Value cannot be represented exactly in DECIMAL(38,12)')
    return number


def _coerce(value, column: Column):
    if value is None:
        if not column.nullable:
            raise WorkflowError('NULL_VALUE', 'A non-nullable column contains missing data')
        return None
    if column.data_type == 'text':
        if not isinstance(value, str):
            raise WorkflowError('TYPE_MISMATCH', 'Text fields require string values')
        return value
    if column.data_type == 'decimal':
        return decimal_value(value)
    if column.data_type == 'integer':
        if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
            raise WorkflowError('TYPE_MISMATCH', 'Integer field contains an invalid value')
        n = decimal_value(value)
        if n != n.to_integral_value():
            raise WorkflowError('TYPE_MISMATCH', 'Integer field contains a non-integer value')
        return int(n)
    if column.data_type == 'boolean':
        if type(value) is not bool:
            raise WorkflowError('TYPE_MISMATCH', 'Boolean fields require true/false values')
        return value
    if isinstance(value, date):
        value = value.isoformat()
    if not isinstance(value, str):
        raise WorkflowError('TYPE_MISMATCH', 'Date fields require ISO date strings')
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise WorkflowError('TYPE_MISMATCH', 'Date fields require ISO date strings') from None


def _columns(schema):
    return {c.name: c for c in schema.columns}


def _table(schema: TableSchema, rows, budget: Budget, deadline: float):
    if not isinstance(rows, list):
        raise WorkflowError('INVALID_TABLE', 'Input tables must supply complete row lists')
    if len(rows) > budget.max_rows or len(rows) * len(schema.columns) > budget.max_cells:
        raise WorkflowError('ROW_BUDGET', 'Complete table exceeds the row/cell budget; no truncated result was produced')
    fields = _columns(schema)
    converted, keys = [], set()
    for index, row in enumerate(rows):
        if index % 1000 == 0 and time.monotonic() > deadline:
            raise WorkflowError('TIMEOUT', 'Workflow exceeded its execution time budget')
        if not isinstance(row, dict) or set(row) != set(fields):
            raise WorkflowError('SCHEMA_MISMATCH', 'Every input row must exactly match the declared columns')
        result = {}
        for name,column in fields.items():
            try:result[name]=_coerce(row[name],column)
            except WorkflowError as error:
                error.details=WorkflowError(error.code,str(error),details={
                    'column':name,'row_index':index+1,'expected_data_type':column.data_type,
                    'actual_type':_actual_type(row[name])}).details
                raise
        key = tuple(result[name] for name in schema.grain)
        if any(value is None for value in key):
            raise WorkflowError('GRAIN_NULL', 'A row-grain key contains a missing value')
        if key in keys:
            raise WorkflowError('GRAIN_DUPLICATE', 'Declared row-grain keys are not unique')
        keys.add(key)
        converted.append(result)
    value = {'schema': schema.model_dump(), 'rows': converted}
    encoded_size = len(canonical(value))
    if encoded_size > budget.max_bytes:
        raise WorkflowError('BYTE_BUDGET', 'Complete table exceeds the byte budget; no truncated result was produced')
    return {'schema': schema, 'rows': converted, '_bytes': encoded_size}


def _require(names, fields):
    if len(names) != len(set(names)) or not set(names) <= set(fields):
        raise WorkflowError('MISSING_COLUMN', 'Operation references missing or duplicate columns')


def _new_schema(schema, columns, grain=None, meaning=None):
    return TableSchema(columns=columns, grain=schema.grain if grain is None else grain,
                       meaning=meaning or schema.meaning, source_ref=schema.source_ref)


def _matches(row, rule: Condition, column: Column):
    value = row[rule.column]
    if rule.operator == 'is_null': return value is None
    if rule.operator == 'not_null': return value is not None
    if value is None: return False
    raw = rule.value if isinstance(rule.value, list) else [rule.value]
    operands = [_coerce(v, column) for v in raw]
    if rule.operator == 'in': return value in operands
    if rule.operator == 'not_in': return value not in operands
    other = operands[0]
    if rule.operator == 'contains':
        if column.data_type != 'text':
            raise WorkflowError('TYPE_MISMATCH', 'Contains filters apply only to text')
        return other in value
    return {'eq': lambda: value == other, 'neq': lambda: value != other,
            'gt': lambda: value > other, 'gte': lambda: value >= other,
            'lt': lambda: value < other, 'lte': lambda: value <= other}[rule.operator]()


def _calculation_semantics(rule: Calculation, fields):
    left, right = fields[rule.left], fields[rule.right]
    if left.data_type not in ('integer', 'decimal') or right.data_type not in ('integer', 'decimal'):
        raise WorkflowError('TYPE_MISMATCH', 'Arithmetic requires declared numeric input columns')
    if left.period != right.period:
        raise WorkflowError('SEMANTIC_MISMATCH', 'Arithmetic inputs have different reporting periods')
    expected_unit = left.unit
    if rule.operation in ('add', 'subtract') and left.unit != right.unit:
        raise WorkflowError('UNIT_MISMATCH', 'Addition and subtraction require identical units')
    if rule.operation == 'multiply':
        expected_unit = right.unit if left.unit == '1' else left.unit if right.unit == '1' else left.unit + '*' + right.unit
    if rule.operation == 'divide':
        expected_unit = '1' if left.unit == right.unit else left.unit if right.unit == '1' else left.unit + '/' + right.unit
    expected_format = 'native'
    if rule.operation in ('add', 'subtract'):
        if left.value_format != right.value_format:
            raise WorkflowError('VALUE_FORMAT_MISMATCH', 'Arithmetic cannot mix percentage fractions and percentage points')
        expected_format = left.value_format
    elif rule.operation == 'multiply':
        if right.unit == '1' and right.value_format == 'native':
            expected_format = left.value_format
        elif left.unit == '1' and left.value_format == 'native':
            expected_format = right.value_format
        elif left.value_format != 'native' or right.value_format != 'native':
            raise WorkflowError('VALUE_FORMAT_MISMATCH', 'Formatted percentage multiplication requires an explicit reviewed conversion')
    elif rule.operation == 'divide':
        if left.unit == right.unit and left.value_format == right.value_format:
            expected_format = 'native'
        elif right.unit == '1' and right.value_format == 'native':
            expected_format = left.value_format
        elif left.value_format != 'native' or right.value_format != 'native':
            raise WorkflowError('VALUE_FORMAT_MISMATCH', 'Formatted percentage division requires an explicit reviewed conversion')
    if rule.output.value_format != expected_format:
        raise WorkflowError('VALUE_FORMAT_MISMATCH', 'Arithmetic output cannot silently change the input value representation')
    if rule.output.data_type not in ('integer', 'decimal') or rule.output.unit != expected_unit or rule.output.period != left.period:
        raise WorkflowError('SEMANTIC_MISMATCH', 'Arithmetic output unit, period, or type is incompatible')
    if rule.output.aggregation == 'additive' and (
            rule.operation not in ('add', 'subtract') or
            left.aggregation != 'additive' or right.aggregation != 'additive'):
        raise WorkflowError('NON_ADDITIVE', 'Arithmetic cannot establish additive output from unproven or non-additive measures')


def _calculate(rule: Calculation, row, fields):
    _calculation_semantics(rule, fields)
    a, b = row[rule.left], row[rule.right]
    if a is None or b is None:
        raise WorkflowError('NULL_VALUE', 'Arithmetic inputs contain missing values')
    with localcontext() as ctx:
        ctx.prec = 100
        a, b = Decimal(a), Decimal(b)
        if rule.operation == 'divide' and b == 0:
            raise WorkflowError('DIVIDE_BY_ZERO', 'Division by zero is not allowed')
        result = {'add': lambda: a + b, 'subtract': lambda: a - b,
                  'multiply': lambda: a * b, 'divide': lambda: a / b}[rule.operation]()
        if rule.rounding_scale is not None:
            result = result.quantize(Decimal(1).scaleb(-rule.rounding_scale), rounding=ROUND_HALF_EVEN)
        return _coerce(result, rule.output)


def _sort_key(row, columns):
    # Nulls last; each declared column has a single validated scalar type.
    return tuple((row[name] is None, row[name]) for name in columns)


def _aggregate(stage: Aggregate, source):
    schema, rows = source['schema'], source['rows']
    fields = _columns(schema)
    _require(stage.by, fields)
    output_columns = [fields[name] for name in stage.by]
    names = list(stage.by)
    for a in stage.aggregations:
        if a.output.name in names:
            raise WorkflowError('DUPLICATE_COLUMN', 'Aggregation output names must be unique')
        names.append(a.output.name)
        if a.column is not None:
            _require([a.column], fields)
        _require(a.order_by, fields)
        if a.function == 'count':
            if a.output.data_type != 'integer' or a.output.unit != '1':
                raise WorkflowError('SEMANTIC_MISMATCH', 'Counts require integer output with unit 1')
        elif a.function == 'concat':
            if fields[a.column].data_type != 'text' or a.output.data_type != 'text':
                raise WorkflowError('TYPE_MISMATCH', 'Concatenation requires text fields')
            col = fields[a.column]
            if (col.unit, col.period, col.aggregation) != (a.output.unit, a.output.period, a.output.aggregation):
                raise WorkflowError('SEMANTIC_MISMATCH', 'Concatenation cannot erase source units, periods, or additivity')
        else:
            col = fields[a.column]
            if col.data_type not in ('decimal', 'integer') or a.output.data_type not in ('decimal', 'integer'):
                raise WorkflowError('TYPE_MISMATCH', 'Numeric aggregates require numeric columns')
            if col.unit != a.output.unit or col.period != a.output.period or col.value_format != a.output.value_format:
                raise WorkflowError('SEMANTIC_MISMATCH', 'Aggregation output unit or period changed')
            if a.function == 'sum' and col.aggregation != 'additive':
                raise WorkflowError('NON_ADDITIVE', 'Summation requires a declared additive measure')
            if a.output.aggregation == 'additive' and col.aggregation != 'additive':
                raise WorkflowError('NON_ADDITIVE', 'Aggregation cannot promote an unproven or non-additive measure to additive')
        output_columns.append(a.output)
    groups = {}
    for row in rows:
        key = tuple(row[name] for name in stage.by)
        groups.setdefault(key, []).append(row)
    if not rows and not stage.by:
        groups[()] = []
    result = []
    for key, members in groups.items():
        output = dict(zip(stage.by, key))
        for a in stage.aggregations:
            selected = members
            if a.function == 'count':
                value = len(selected) if a.column is None else sum(r[a.column] is not None for r in selected)
            elif a.function == 'concat':
                selected = sorted(members, key=lambda r: _sort_key(r, a.order_by or schema.grain))
                # Missing text is not an empty string: fail rather than silently omit.
                if any(r[a.column] is None for r in selected):
                    raise WorkflowError('NULL_VALUE', 'Concatenation contains missing text values')
                value = a.delimiter.join(r[a.column] for r in selected)
            else:
                values = [r[a.column] for r in selected]
                if any(v is None for v in values):
                    raise WorkflowError('NULL_VALUE', 'Numeric aggregation contains missing values')
                if a.function == 'sum':
                    with localcontext() as ctx:
                        ctx.prec = 100
                        value = sum((Decimal(v) for v in values), Decimal(0))
                elif not values:
                    value = None
                else:
                    value = min(values) if a.function == 'min' else max(values)
            output[a.output.name] = _coerce(value, a.output)
        result.append(output)
    result.sort(key=lambda row: _sort_key(row, stage.by))
    return _new_schema(schema, output_columns, stage.by, stage.meaning), result


# This is intentionally a function allowlist, not a network/file-function denylist.
SQL_FUNCTIONS = {
    'SUM', 'COUNT', 'MIN', 'MAX', 'AVG', 'COALESCE', 'NULLIF', 'CASE', 'IF', 'CAST', 'TRY_CAST',
    'ROUND', 'ABS', 'CONCAT', 'CONCAT_WS', 'STRING_AGG', 'GROUP_CONCAT', 'ROW_NUMBER', 'RANK',
    'DENSE_RANK', 'DATE', 'DATE_TRUNC', 'EXTRACT', 'LOWER', 'UPPER', 'TRIM', 'LENGTH', 'SUBSTRING',
    'REPLACE', 'GREATEST', 'LEAST', 'FIRST_VALUE', 'LAST_VALUE', 'LAG', 'LEAD', 'ISNULL',
}


def _validate_exact_sql_numeric(tree):
    """Reject known DuckDB floating intermediates before CAST can hide them.

    This is a conservative operation gate, not a general SQL type inference
    engine. Decimal inputs alone do not make division/AVG exact in DuckDB.
    """
    integer_date_parts={'YEAR','MONTH','DAY','DAYOFMONTH','DAYOFWEEK','DAYOFYEAR','DOW','DOY','ISODOW',
        'WEEK','WEEKOFYEAR','ISOYEAR','YEARWEEK','QUARTER','DECADE','CENTURY','MILLENNIUM','ERA',
        'HOUR','MINUTE','SECOND','MILLISECOND','MILLISECONDS','MICROSECOND','MICROSECONDS',
        'TIMEZONE','TIMEZONE_HOUR','TIMEZONE_MINUTE'}
    floating_types={'FLOAT','DOUBLE','REAL','FLOAT32','FLOAT64','BINARY_FLOAT','BINARY_DOUBLE','DECFLOAT'}
    for node in tree.walk():
        unsafe=isinstance(node,(exp.Div,exp.IntDiv,exp.Avg))
        if isinstance(node,(exp.Cast,exp.TryCast)):
            target=node.args.get('to')
            unsafe=unsafe or isinstance(target,exp.DataType) and any(
                str(data_type.this.value).upper() in floating_types for data_type in target.find_all(exp.DataType))
        if isinstance(node,exp.Literal) and not node.is_string:
            literal=str(node.this)
            # Excessively long fixed-point literals also become DOUBLE in
            # DuckDB, even when they contain no scientific-notation marker.
            unsafe=unsafe or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?',literal) or len(literal.replace('.',''))>38
        if isinstance(node,exp.Extract):
            unsafe=unsafe or node.this.name.upper() not in integer_date_parts
        if unsafe:
            raise WorkflowError('SQL_INEXACT_NUMERIC',
                'SQL中的该数值运算可能经过二进制浮点，CAST回DECIMAL也不能恢复精度；'
                '请用精确十进制字面量，均值先aggregate求和与计数，比率或除法改用compute并明确舍入位数')


def validate_sql(sql: str, allowed_tables: set[str], *, allowed_schema: str | None = None) -> str:
    try:
        trees = sqlglot.parse(sql, read='duckdb')
    except sqlglot.errors.ParseError:
        raise WorkflowError('UNSAFE_SQL', 'SQL could not be parsed as a supported SELECT') from None
    if len(trees) != 1 or not isinstance(trees[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise WorkflowError('UNSAFE_SQL', 'Only one SELECT query is allowed')
    tree = trees[0]
    forbidden = (exp.Insert, exp.Delete, exp.Update, exp.Create, exp.Drop, exp.Command, exp.Into,
                 exp.Copy, exp.Attach, exp.Pragma, exp.Unnest, exp.Lateral)
    if any(tree.find(t) for t in forbidden):
        raise WorkflowError('UNSAFE_SQL', 'SQL includes unsupported operations')
    if any(node.args.get('recursive') for node in tree.find_all(exp.With)):
        raise WorkflowError('UNSAFE_SQL', 'Recursive SQL is outside the workflow budget contract')
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    tables = list(tree.find_all(exp.Table))
    base_tables = []
    for table in tables:
        if (not isinstance(table.this, exp.Identifier) or table.db not in ('',allowed_schema)
                or table.catalog or table.name not in allowed_tables | ctes):
            raise WorkflowError('UNSAFE_SQL', 'SQL can read only explicitly supplied workflow tables')
        if table.name in allowed_tables and table.name not in ctes:
            base_tables.append(table.name)
    if not base_tables:
        raise WorkflowError('UNSAFE_SQL', 'SQL must reference an explicitly supplied input table')
    for function in tree.find_all(exp.Func):
        name = function.name.upper() if isinstance(function, exp.Anonymous) else function.sql_name().upper()
        if name not in SQL_FUNCTIONS:
            raise WorkflowError('UNSAFE_SQL', 'SQL contains a function outside the approved deterministic set')
    _validate_exact_sql_numeric(tree)
    for window in tree.find_all(exp.Window):
        if not window.args.get('order'):
            raise WorkflowError('NONDETERMINISTIC_SQL', 'Window functions require explicit ordering')
    return tree.sql(dialect='duckdb')


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _sql(stage: SQL, tables, budget, deadline):
    sql = validate_sql(stage.sql, set(stage.inputs))
    config = {'enable_external_access': False, 'allow_unsigned_extensions': False,
              'memory_limit': '128MB', 'threads': 1}
    with duckdb.connect(config=config) as con:
        for name in stage.inputs:
            table = tables[name]
            types = {'text': 'VARCHAR', 'decimal': 'DECIMAL(38,12)', 'integer': 'HUGEINT',
                     'boolean': 'BOOLEAN', 'date': 'DATE'}
            fields = table['schema'].columns
            con.execute('CREATE TABLE ' + _quote(name) + '(' + ','.join(
                _quote(c.name) + ' ' + types[c.data_type] for c in fields) + ')')
            if table['rows']:
                con.executemany('INSERT INTO ' + _quote(name) + ' VALUES (' + ','.join('?' for _ in fields) + ')',
                                [[r[c.name] for c in fields] for r in table['rows']])
            if time.monotonic() > deadline:
                raise WorkflowError('TIMEOUT', 'Workflow exceeded its execution time budget')
        wait = min(budget.sql_timeout_seconds, max(0.001, deadline - time.monotonic()))
        timer = threading.Timer(wait, con.interrupt)
        timer.start()
        try:
            cursor = con.execute(sql)
            names = [d[0] for d in cursor.description]
            if names != [c.name for c in stage.output_schema.columns]:
                raise WorkflowError('SCHEMA_MISMATCH', 'SQL output columns do not match the explicit semantic schema')
            values = cursor.fetchmany(budget.max_rows + 1)
            if len(values) > budget.max_rows:
                raise WorkflowError('ROW_BUDGET', 'SQL output exceeds its row budget; no truncated output was returned')
            rows = [dict(zip(names, row)) for row in values]
        except duckdb.InterruptException:
            raise WorkflowError('TIMEOUT', 'SQL exceeded its execution time budget') from None
        except duckdb.Error:
            raise WorkflowError('SQL_FAILED', 'SQL failed validation or execution; no output was published') from None
        finally:
            timer.cancel()
    return stage.output_schema, rows


def _operate(stage, tables, budget, deadline):
    if isinstance(stage, SQL):
        return _sql(stage, tables, budget, deadline)
    source = tables[dependencies_for(stage)[0]]
    schema, rows = source['schema'], source['rows']
    fields = _columns(schema)
    if isinstance(stage, Normalize):
        _require(list(dict.fromkeys(rule.column for rule in stage.rules)), fields)
        outputs = [rule.output.name for rule in stage.rules]
        if len(set(outputs)) != len(outputs) or set(outputs) & set(fields):
            raise WorkflowError('DUPLICATE_COLUMN', 'Normalization retains original text columns; outputs must have new unique names')
        for rule in stage.rules:
            if fields[rule.column].data_type != 'text':
                raise WorkflowError('TYPE_MISMATCH', 'Explicit text normalization requires a text source column')
            if fields[rule.column].period != rule.output.period:
                raise WorkflowError('SEMANTIC_MISMATCH', 'Normalization cannot silently change the reporting period')
            if fields[rule.column].unit and fields[rule.column].unit != rule.source_unit:
                raise WorkflowError('UNIT_MISMATCH', 'Normalization source unit must match the preserved input unit')
            if fields[rule.column].aggregation == 'non_additive' and rule.output.aggregation == 'additive':
                raise WorkflowError('NON_ADDITIVE', 'Normalization cannot turn a non-additive source into an additive measure')
        result = []
        for index, row in enumerate(rows):
            if index % 1000 == 0 and time.monotonic() > deadline:
                raise WorkflowError('TIMEOUT', 'Normalization exceeded its execution time budget')
            normalized = dict(row)
            for rule in stage.rules:
                try:
                    parsed = parse_localized_value(row[rule.column], kind=rule.kind, currency=rule.currency,
                        unit=rule.source_unit, unit_multiplier=rule.unit_multiplier, date_format=rule.date_format,
                        missing_policy=rule.missing_policy, missing_tokens=rule.missing_tokens)
                except WorkbookReadError as error:
                    raise WorkflowError(error.code, str(error)) from error
                normalized[rule.output.name] = _coerce(parsed['value'], rule.output)
            result.append(normalized)
        return _new_schema(schema, list(schema.columns) + [rule.output for rule in stage.rules]), result
    if isinstance(stage, (Select, Drop)):
        _require(stage.columns, fields)
        names = stage.columns if isinstance(stage, Select) else [n for n in fields if n not in stage.columns]
        if not names or not set(schema.grain) <= set(names):
            raise WorkflowError('GRAIN_LOST', 'Selecting columns cannot discard row-grain identifiers; aggregate explicitly')
        return _new_schema(schema, [fields[n] for n in names]), [{n: r[n] for n in names} for r in rows]
    if isinstance(stage, Filter):
        _require(list(dict.fromkeys(r.column for r in stage.conditions)), fields)
        return schema, [dict(row) for row in rows if all(_matches(row, rule, fields[rule.column]) for rule in stage.conditions)]
    if isinstance(stage, Reclassify):
        _require([stage.column], fields)
        if fields[stage.column].data_type != 'text' or stage.output.data_type != 'text':
            raise WorkflowError('TYPE_MISMATCH', 'Reclassification maps text categories')
        original = fields[stage.column]
        if (original.unit, original.period, original.aggregation) != (stage.output.unit, stage.output.period, stage.output.aggregation):
            raise WorkflowError('SEMANTIC_MISMATCH', 'Reclassification cannot erase source units, periods, or additivity')
        if stage.output.name in fields and stage.output.name != stage.column:
            raise WorkflowError('DUPLICATE_COLUMN', 'Reclassification would overwrite an existing field')
        if stage.column in schema.grain and stage.output.name == stage.column:
            raise WorkflowError('GRAIN_LOST', 'Reclassification cannot overwrite an identifying grain key')
        columns = [stage.output if c.name == stage.output.name else c for c in schema.columns]
        if stage.output.name not in fields:
            columns.append(stage.output)
        result = []
        for row in rows:
            value = row[stage.column]
            if value not in stage.mapping and stage.unmapped == 'error':
                raise WorkflowError('UNMAPPED_VALUE', 'Reclassification has unmapped values')
            result.append(dict(row, **{stage.output.name: stage.mapping.get(value, value)}))
        return _new_schema(schema, columns), result
    if isinstance(stage, Compute):
        columns = list(schema.columns)
        result = [dict(row) for row in rows]
        for rule in stage.calculations:
            _require([rule.left] if rule.left == rule.right else [rule.left, rule.right], fields)
            if rule.output.name in fields:
                raise WorkflowError('DUPLICATE_COLUMN', 'Arithmetic output cannot overwrite an existing field')
            _calculation_semantics(rule, fields)
            for row in result:
                row[rule.output.name] = _calculate(rule, row, fields)
            columns.append(rule.output)
            fields[rule.output.name] = rule.output
        return _new_schema(schema, columns), result
    if isinstance(stage, Aggregate):
        return _aggregate(stage, source)
    if isinstance(stage, Lookup):
        right = tables[stage.right]
        right_fields = _columns(right['schema'])
        _require([stage.left_key], fields)
        _require(list(dict.fromkeys([stage.right_key] + list(stage.outputs))), right_fields)
        left_key, right_key = fields[stage.left_key], right_fields[stage.right_key]
        if (left_key.data_type, left_key.unit, left_key.period, left_key.value_format) != (right_key.data_type, right_key.unit, right_key.period, right_key.value_format):
            raise WorkflowError('SEMANTIC_MISMATCH', 'Lookup keys have incompatible types, units, or reporting periods')
        outputs = [value.name for value in stage.outputs.values()]
        if len(outputs) != len(set(outputs)) or set(outputs) & set(fields):
            raise WorkflowError('DUPLICATE_COLUMN', 'Lookup output names would overwrite columns')
        for column, output in stage.outputs.items():
            original = right_fields[column]
            if (original.data_type, original.unit, original.period, original.value_format, original.aggregation) != (output.data_type, output.unit, output.period, output.value_format, output.aggregation):
                raise WorkflowError('SEMANTIC_MISMATCH', 'Lookup cannot silently change source type, unit, period, or additivity')
        index = {}
        for row in right['rows']:
            key = row[stage.right_key]
            if key is None or key in index:
                raise WorkflowError('LOOKUP_CARDINALITY', 'Lookup right key must be non-null and unique; one-to-many mapping is ambiguous')
            index[key] = row
        result = []
        for row in rows:
            match = index.get(row[stage.left_key])
            if match is None and stage.unmapped == 'error':
                raise WorkflowError('UNMAPPED_VALUE', 'Lookup has unmatched left records')
            value = dict(row)
            value.update({output.name: match[column] if match is not None else None for column, output in stage.outputs.items()})
            result.append(value)
        return _new_schema(schema, list(schema.columns) + list(stage.outputs.values())), result
    if isinstance(stage, UnionRows):
        for name in stage.inputs[1:]:
            other = tables[name]['schema']
            if schema.columns != other.columns or schema.grain != other.grain or schema.meaning != other.meaning:
                raise WorkflowError('SEMANTIC_MISMATCH', 'Row concatenation requires identical column meanings, units, and grain')
        return schema, [dict(row) for name in stage.inputs for row in tables[name]['rows']]
    raise WorkflowError('UNSUPPORTED_STAGE', 'Unsupported workflow stage')


def execute(plan: WorkflowPlan | dict, inputs: dict[str, list[dict[str, Any]]], *,
            on_stage: Callable[[dict], None] | None = None) -> dict:
    """Execute a complete plan against explicit full inputs in an isolated run.

    Monetary inputs and returned values use exact decimal strings at the public
    boundary. ``on_stage`` receives receipts only, not raw rows. Exceptions mean
    no final output exists; callers must not publish intermediate stage data.
    """
    plan = WorkflowPlan.model_validate(plan)
    if set(inputs) != set(plan.inputs):
        raise WorkflowError('INPUT_MISMATCH', 'Supplied tables do not match declared workflow inputs')
    started = time.monotonic()
    deadline = started + plan.budget.timeout_seconds
    tables, input_hashes, receipts = {}, {}, []
    for name, schema in plan.inputs.items():
        try:tables[name] = _table(schema, inputs[name], plan.budget, deadline)
        except WorkflowError as error:
            error.stage_id='input:'+name
            raise
        input_hashes[name] = digest({'schema': schema.model_dump(), 'rows': tables[name]['rows']})
        if sum(table['_bytes'] for table in tables.values()) > plan.budget.max_bytes:
            raise WorkflowError('BYTE_BUDGET', 'Combined input tables exceed the serialized data budget')
    for stage in plan.stages:
        if time.monotonic() > deadline:
            raise WorkflowError('TIMEOUT', 'Workflow exceeded its execution time budget', stage.id)
        dependencies = dependencies_for(stage)
        try:
            schema, rows = _operate(stage, tables, plan.budget, deadline)
            table = _table(schema, rows, plan.budget, deadline)
        except WorkflowError as error:
            error.stage_id = stage.id
            raise
        if sum(cached['_bytes'] for cached in tables.values()) + table['_bytes'] > plan.budget.max_bytes:
            raise WorkflowError('BYTE_BUDGET', 'Retained dependency tables exceed the serialized data budget', stage.id)
        table_hash = digest({'schema': schema.model_dump(), 'rows': table['rows']})
        tables[stage.id] = table
        receipt = {'stage_id': stage.id, 'kind': stage.kind, 'stage_sha256': digest(stage.model_dump()),
                   'inputs': [{'table': name, 'rows': len(tables[name]['rows']),
                               'sha256': input_hashes.get(name) or next(r['output_sha256'] for r in receipts if r['stage_id'] == name)}
                              for name in dependencies],
                   'output_rows': len(table['rows']), 'output_sha256': table_hash,
                   'schema_sha256': digest(schema.model_dump()),
                   'checks': {'complete_table': True, 'grain_unique': True, 'types_and_units_declared': True,
                              'sql_output_semantics': 'declared_requires_review' if isinstance(stage, SQL) else 'typed_constraints_checked'},
                   'lineage': stage.model_dump()}
        receipts.append(receipt)
        if isinstance(stage, Normalize):
            receipt['checks']['localized_parsing'] = 'explicit_policy; original_text_columns_retained; business_semantics_require_review'
        if on_stage is not None:
            on_stage(json.loads(canonical(receipt)))
    output = tables[plan.output]
    # Round-trip canonical JSON deliberately converts Decimal values to strings.
    exported = json.loads(canonical({'schema': output['schema'].model_dump(), 'rows': output['rows']}))
    result_hash = digest({'schema': output['schema'].model_dump(), 'rows': output['rows']})
    return {'version': 1, 'plan_sha256': digest(plan.model_dump()), 'input_sha256': input_hashes,
            'output': {**exported, 'row_count': len(output['rows']), 'sha256': result_hash},
            'stages': receipts, 'complete': True,
            'requires_semantic_review': any(isinstance(stage, (SQL, Normalize)) for stage in plan.stages),
            'scope': 'explicit_semantic_tables; isolated_typed_operations; full_bounded_output; not_arbitrary_python'}
