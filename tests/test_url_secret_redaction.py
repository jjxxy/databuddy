import pytest

from agent_service.conversation import redact


HIDDEN = '已提供连接信息（内容已隐藏并加密保存）'


@pytest.mark.parametrize('message', [
    '请访问 https://example.invalid/api',
    '来源 `http://127.0.0.1:8765/api/refunds?page=1&page_size=10`。',
    '请读取 https://example.invalid/data?offset=100&limit=50',
    '读取（https://example.invalid/data?page=2）之后继续',
])
def test_plain_urls_and_numeric_paging_remain_usable(message):
    assert redact(message) == message


@pytest.mark.parametrize('message', [
    'https://example.invalid/file?X-Amz-Signature=synthetic-signature',
    'https://example.invalid/file?sig=synthetic-signature',
    'https://example.invalid/file?X-Goog-Credential=synthetic-credential',
    'https://example.invalid/file?code=synthetic-oauth-code',
    'https://example.invalid/file?arbitrary=synthetic-private-value',
    'https://example.invalid/file?%73ig=synthetic-signature',
    'https://example.invalid/file?page=1&opaque=synthetic-private-value',
    'https://example.invalid/file?page=synthetic-private-value',
    'https://example.invalid/file#synthetic-private-fragment',
    'https://synthetic-user:synthetic-pass@example.invalid/file',
    'https://example.invalid/file?badquery',
])
def test_arbitrary_query_fragment_and_userinfo_are_hidden(message):
    assert redact(message) == HIDDEN
