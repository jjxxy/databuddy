"""Deterministic lineage for Atlas' constrained generator, never inferred SQL."""
import copy
import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def build(source, fields, spec, joins):
    nodes = {}
    for field in fields:
        if field.get('derived'):
            continue
        nodes[field['name']] = {
            'column': field['name'], 'kind': 'source',
            'inputs': [{'source_id': field.get('source_id', source['id']),
                        'column': field.get('original_name', field['name'])}],
            'operations': [],
        }
    source_inputs = [{'source_id': source['id'], 'column': f['name']}
                     for f in source['profile']['fields']]
    nodes['record_hash'] = {'column': 'record_hash', 'kind': 'system',
                            'inputs': source_inputs, 'operations': [{'kind': 'record_hash'}]}
    for column in spec.get('trim_fields', []):
        nodes[column]['operations'].append({'kind': 'trim_blank_to_null'})
    for rule in spec.get('computations', []):
        parents = [nodes[rule[k]] for k in ('left_field', 'right_field')]
        inputs = { (i['source_id'], i['column']): i
                   for parent in parents for i in parent['inputs'] }
        operations = [copy.deepcopy(op) for parent in parents for op in parent['operations']]
        operations.append({'kind': 'arithmetic', **{k: rule[k] for k in
                           ('name', 'operation', 'left_field', 'right_field')}})
        nodes[rule['name']] = {'column': rule['name'], 'kind': 'computed',
                               'inputs': list(inputs.values()), 'operations': operations}
    key = spec.get('primary_key')
    nodes['object_id'] = {'column': 'object_id', 'kind': 'system',
                          'inputs': copy.deepcopy(nodes[key]['inputs'] if key else source_inputs),
                          'operations': [{'kind': 'sha256_primary_key' if key else 'record_hash_identity'}]}
    row_rules = {
        'filters': copy.deepcopy(spec.get('filters', [])),
        'joins': [{k: j[k] for k in ('source_id', 'left_field', 'right_field', 'required_match')}
                  for j in joins],
        'deduplication': 'primary_key_validated' if key else 'record_hash',
    }
    summary = []
    if spec.get('amount_field'):
        amount = nodes[spec['amount_field']]
        group = nodes[spec['group_by']]
        summary = [
            {'column': 'category', 'inputs': copy.deepcopy(group['inputs']),
             'operations': copy.deepcopy(group['operations']) + [{'kind': 'cast_varchar_group_by', 'field': spec['group_by']}]},
            {'column': 'amount_total', 'inputs': copy.deepcopy(amount['inputs']),
             'operations': copy.deepcopy(amount['operations']) + [{'kind': 'sum', 'field': spec['amount_field'], 'cast': 'DECIMAL(38,12)'}]},
            {'column': 'record_count', 'inputs': [], 'operations': [{'kind': 'count_rows'}]},
        ]
    return {'version': 1, 'method': 'generated_transform_spec',
            'columns': list(nodes.values()), 'summary': summary, 'row_rules': row_rules,
            'scope': 'generated_rules_only; row_rules_affect_all_outputs; not_arbitrary_sql_analysis'}
