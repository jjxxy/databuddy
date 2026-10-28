"""CSV source reads must not silently invent missing financial fields."""
import hashlib

import pytest

from agent_service import connectors


def source_file(tmp_path, monkeypatch, payload):
    uploads = tmp_path / 'uploads'
    uploads.mkdir(exist_ok=True)
    (uploads / 'input.csv').write_bytes(payload)
    monkeypatch.setattr(connectors, 'DATA', tmp_path)
    monkeypatch.setattr(connectors, 'unseal', lambda config: config)
    return {'name': 'input.csv', 'kind': 'file',
            'secret_config': {'stored_name': 'input.csv'},
            'content_hash': hashlib.sha256(payload).hexdigest()}


@pytest.mark.parametrize('payload', [
    b'customer,amount\nAlice\n',
    b'customer,amount\nAlice,10,unexpected\n',
])
def test_wrong_number_of_fields_is_rejected_on_verified_source(tmp_path, monkeypatch, payload):
    source = source_file(tmp_path, monkeypatch, payload)
    for limit in (10, connectors.MAX_ROWS):
        with pytest.raises(connectors.SourceError, match='列数与表头不一致'):
            connectors.file_rows(source, limit)


def test_explicit_empty_field_and_quoted_linebreak_are_preserved(tmp_path, monkeypatch):
    payload = '客户,金额,备注\n甲,,"行一,行二\n行三"\n乙,9007199254740993,ok\n'.encode('utf-8')
    source = source_file(tmp_path, monkeypatch, payload)
    rows, evidence = connectors.file_rows(source, connectors.MAX_ROWS)
    assert rows == [{'客户': '甲', '金额': '', '备注': '行一,行二\n行三'},
                    {'客户': '乙', '金额': '9007199254740993', '备注': 'ok'}]
    assert evidence['source_integrity'] == 'verified'


def test_gb18030_source_retains_chinese_and_decimal_text(tmp_path, monkeypatch):
    source = source_file(tmp_path, monkeypatch, '客户,金额\r\n甲,118.15\r\n'.encode('gb18030'))
    rows, evidence = connectors.file_rows(source, connectors.MAX_ROWS)
    assert rows == [{'客户': '甲', '金额': '118.15'}]
    assert evidence['schema_labels'] == ['客户', '金额']


@pytest.mark.parametrize('payload', [
    b'customer,amount\n"Alice,10\n',
    b'customer,amount\n"Alice"oops,10\n',
])
def test_invalid_quotes_are_rejected_instead_of_consuming_later_rows(tmp_path, monkeypatch, payload):
    source = source_file(tmp_path, monkeypatch, payload)
    with pytest.raises(connectors.SourceError, match='格式错误'):
        connectors.file_rows(source, connectors.MAX_ROWS)


@pytest.mark.parametrize('payload', [b'customer,,amount\nAlice,,10\n',
                                    b'customer,  ,amount\nAlice,,10\n',
                                    b'customer,customer\nA,B\n'])
def test_empty_or_duplicate_column_labels_are_rejected(tmp_path, monkeypatch, payload):
    source = source_file(tmp_path, monkeypatch, payload)
    with pytest.raises(connectors.SourceError, match='表头为空或有重复'):
        connectors.file_rows(source, 10)


def test_invalid_text_encoding_returns_readable_error(tmp_path, monkeypatch):
    source = source_file(tmp_path, monkeypatch, b'\xff\xff\xff')
    with pytest.raises(connectors.SourceError, match='编码无法识别'):
        connectors.file_rows(source, 10)
