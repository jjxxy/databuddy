"""Configured, observable browser exports for a trusted isolated browser context.

This module never creates a browser session or acquires account credentials.
The deployment must supply a Page whose network policy has already been verified.
Verifying visible filter controls is distinct from verifying the exported rows.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import uuid
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from filelock import FileLock

from .models import StrictModel
from .platform_files import fsync_file


class BrowserReportError(ValueError):
    def __init__(self, message: str, code: str = 'BROWSER_REPORT_FAILED'):
        super().__init__(message)
        self.code = code
        self.retryable = False


class BrowserUnavailable(BrowserReportError):
    pass


class FilterControl(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    selector: str = Field(min_length=1, max_length=500)
    kind: Literal['select', 'text', 'date']
    value: str = Field(max_length=2000)

    @model_validator(mode='after')
    def validate_control(self):
        if self.kind == 'date':
            from datetime import date
            try:
                date.fromisoformat(self.value)
            except ValueError:
                raise ValueError('日期筛选值必须是 ISO 日期') from None
        return self


class BrowserRecipe(StrictModel):
    url: str = Field(min_length=1, max_length=4000)
    iframe_selector: str | None = Field(default=None, min_length=1, max_length=500)
    filters: list[FilterControl] = Field(default_factory=list, max_length=30)
    query_button_selector: str | None = Field(default=None, min_length=1, max_length=500)
    export_selector: str = Field(min_length=1, max_length=500)
    context_selector: str = Field(default='body', min_length=1, max_length=500)
    required_context_text: list[str] = Field(min_length=1, max_length=20)
    ready_selector: str | None = Field(default=None, min_length=1, max_length=500)
    timeout_ms: int = Field(default=30000, ge=1000, le=120000)
    download_timeout_ms: int = Field(default=120000, ge=1000, le=300000)
    max_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)

    @model_validator(mode='after')
    def validate_recipe(self):
        parsed = urlsplit(self.url)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('报表必须使用不含内嵌凭证的 HTTP(S) 地址')
        if len({f.label for f in self.filters}) != len(self.filters):
            raise ValueError('筛选项名称不能重复')
        if len({f.selector for f in self.filters}) != len(self.filters):
            raise ValueError('筛选项选择器不能重复')
        if any(not text.strip() or len(text) > 2000 for text in self.required_context_text):
            raise ValueError('报表上下文必须为明确的非空文字')
        return self


def availability():
    """A Python import is not evidence of an authenticated, isolated browser."""
    return {'playwright_python_installed': importlib.util.find_spec('playwright') is not None,
            'managed_browser_configured': False, 'ready': False,
            'requires': 'owner_supplied_authenticated_page_and_verified_network_policy',
            'real_report_site_verified': False}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def _file_hash(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _unique(scope, selector, timeout):
    locator = scope.locator(selector)
    if locator.count() > 1:
        raise BrowserReportError('已配置控件没有唯一匹配，请核对报表页面版本', 'BROWSER_LOCATOR_AMBIGUOUS')
    locator.wait_for(state='visible', timeout=timeout)
    if locator.count() != 1:
        raise BrowserReportError('已配置控件没有唯一匹配，请核对报表页面版本', 'BROWSER_LOCATOR_AMBIGUOUS')
    return locator


def _verify_filter(locator, control, timeout):
    # A report recipe cannot be used as an alternate credential-entry interface.
    input_type = (locator.get_attribute('type', timeout=timeout) or '').lower()
    if input_type in ('password', 'file', 'hidden', 'submit', 'button'):
        raise BrowserReportError('该控件不是允许的报表筛选输入', 'BROWSER_FILTER_UNSAFE')
    if locator.input_value(timeout=timeout) != control.value:
        raise BrowserReportError('筛选器回读值与要求不一致，未执行导出', 'BROWSER_FILTER_MISMATCH')


def _atomic_json(path, value):
    temp = path.with_name(path.name + '.part-' + uuid.uuid4().hex)
    try:
        with temp.open('x', encoding='utf-8') as stream:
            os.chmod(temp, 0o600)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _check_browser_network():
    from .network_policy import mode
    if mode() == 'local_only':
        # A supplied Page's first URL cannot prove isolation of redirects,
        # iframes, WebSockets, downloads and other browser-side requests.
        raise BrowserUnavailable('仅本地连接模式不启用浏览器报表导出；请使用本地文件或已允许的接口数据源',
                                 'BROWSER_LOCAL_ONLY_UNAVAILABLE')


def _export_with_page_locked(page, recipe: BrowserRecipe | dict, destination: str | Path, *,
                     validator: Callable[[Path], dict], network_policy_verified: bool = False):
    """Execute one configured export against an existing synchronous Playwright Page.

    ``validator`` is mandatory and must validate the completed file against the
    requested format, expected columns/rows, and export filter/context evidence.
    It can be ``lambda path: validate_download(path, report_request)``.

    Network isolation is a deployment precondition, not something this function
    infers from a Page. No login, arbitrary JavaScript or coordinate clicks occur.
    Query and export buttons are pressed at most once; failures are not retried.
    Existing completed receipt/file pairs can be reused without touching the site.
    """
    _check_browser_network()
    recipe = BrowserRecipe.model_validate(recipe)
    if network_policy_verified is not True:
        raise BrowserUnavailable('浏览器报表执行器未配置已验证的隔离网络策略', 'BROWSER_NOT_CONFIGURED')
    if not callable(validator):
        raise ValueError('浏览器导出必须提供下载内容校验器')
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError('下载目标不能是符号链接')
    destination.parent.mkdir(parents=True, exist_ok=True)
    receipt_path = destination.with_name(destination.name + '.browser-receipt.json')
    if receipt_path.is_symlink():
        raise ValueError('下载回执不能是符号链接')
    recipe_sha256 = _digest(recipe.model_dump())
    if destination.exists() or receipt_path.exists():
        if not destination.is_file() or not receipt_path.is_file():
            raise BrowserReportError('已有下载文件缺少完整回执，请先核对；未重复触发导出', 'BROWSER_EXPORT_UNKNOWN')
        old = json.loads(receipt_path.read_text())
        if old.get('recipe_sha256') != recipe_sha256 or old.get('content_sha256') != _file_hash(destination):
            raise BrowserReportError('已有下载与当前要求或回执不一致，未覆盖或重复导出', 'BROWSER_EXPORT_CONFLICT')
        validation = validator(destination)
        return dict(old, validation=validation, reused=True)

    part = destination.with_name(destination.name + '.part-' + uuid.uuid4().hex)
    try:
        # DNS and egress restrictions must also cover redirects, iframes, downloads,
        # WebSockets and browser-side requests in the supplied context.
        page.goto(recipe.url, wait_until='domcontentloaded', timeout=recipe.timeout_ms)
        requested = urlsplit(recipe.url)
        actual = urlsplit(page.url)
        def origin(parsed):
            return (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
        if origin(actual) != origin(requested):
            raise BrowserReportError('报表页面跳转到其他来源，需先核对登录和访问范围', 'BROWSER_ORIGIN_CHANGED')
        scope = page
        if recipe.iframe_selector:
            _unique(page, recipe.iframe_selector, recipe.timeout_ms)
            scope = page.frame_locator(recipe.iframe_selector)
        selected = {}
        for control in recipe.filters:
            locator = _unique(scope, control.selector, recipe.timeout_ms)
            input_type = (locator.get_attribute('type', timeout=recipe.timeout_ms) or '').lower()
            if input_type in ('password', 'file', 'hidden', 'submit', 'button'):
                raise BrowserReportError('该控件不是允许的报表筛选输入', 'BROWSER_FILTER_UNSAFE')
            if control.kind == 'select':
                locator.select_option(value=control.value, timeout=recipe.timeout_ms)
            else:
                locator.fill(control.value, timeout=recipe.timeout_ms)
            _verify_filter(locator, control, recipe.timeout_ms)
            selected[control.label] = _digest(control.value)
        if recipe.query_button_selector:
            _unique(scope, recipe.query_button_selector, recipe.timeout_ms).click(timeout=recipe.timeout_ms)
        if recipe.ready_selector:
            _unique(scope, recipe.ready_selector, recipe.timeout_ms)
        # Recheck after the query/render stage; a rerender can reset an input.
        for control in recipe.filters:
            _verify_filter(_unique(scope, control.selector, recipe.timeout_ms), control, recipe.timeout_ms)
        context_text = _unique(scope, recipe.context_selector, recipe.timeout_ms).inner_text(timeout=recipe.timeout_ms)
        if any(text not in context_text for text in recipe.required_context_text):
            raise BrowserReportError('报表页面缺少要求的期间、单位或对象上下文，未执行导出', 'BROWSER_CONTEXT_MISMATCH')
        export = _unique(scope, recipe.export_selector, recipe.timeout_ms)
        # Listening before click avoids missing a quick iframe-generated download.
        with page.expect_download(timeout=recipe.download_timeout_ms) as download_info:
            export.click(timeout=recipe.timeout_ms)
        download = download_info.value
        if download.failure() is not None:
            raise BrowserReportError('浏览器未完成下载，未将部分文件视为结果', 'BROWSER_DOWNLOAD_FAILED')
        download.save_as(str(part))
        if not part.is_file() or part.stat().st_size == 0 or part.stat().st_size > recipe.max_bytes:
            raise BrowserReportError('下载为空或超过体积限制', 'BROWSER_DOWNLOAD_SIZE')
        os.chmod(part, 0o600)
        validation = validator(part)
        actual_hash = _file_hash(part)
        if validation.get('content_sha256') and validation['content_sha256'] != actual_hash:
            raise BrowserReportError('内容校验器返回的文件摘要不一致', 'BROWSER_DOWNLOAD_CHANGED')
        receipt = {'recipe_sha256': recipe_sha256, 'content_sha256': actual_hash,
                   'bytes': part.stat().st_size, 'browser_filters_verified': True,
                   'browser_filter_value_sha256': selected, 'browser_context_observed': True,
                   'browser_context_sha256': _digest(context_text),
                   'validation': validation, 'reused': False,
                   'scope': 'ui_controls_verified; export_content_requires_independent_validation; single_export_no_auto_retry'}
        from .locks import assert_owned
        assert_owned()
        fsync_file(part)
        part.replace(destination)
        _atomic_json(receipt_path, receipt)
        return receipt
    except BrowserReportError:
        raise
    except Exception:
        # Playwright exceptions may contain credential-bearing navigation URLs.
        raise BrowserReportError('浏览器报表导出未完成，请核对选择器、登录状态、页面结构和下载权限', 'BROWSER_REPORT_FAILED') from None
    finally:
        part.unlink(missing_ok=True)


def export_with_page(page, recipe: BrowserRecipe | dict, destination: str | Path, *,
                     validator: Callable[[Path], dict], network_policy_verified: bool = False):
    """Export through an owner-supplied authenticated and network-isolated Page.

    This is a product adapter, not an independently configured browser service.
    See ``_export_with_page_locked`` for the full execution/validation contract.
    """
    _check_browser_network()
    destination = Path(destination).absolute()
    if any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ValueError('下载目标不能经过符号链接')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Serializes duplicate invocations before any page action or export click.
    with FileLock(str(destination) + '.browser.lock', timeout=10):
        return _export_with_page_locked(page, recipe, destination, validator=validator,
                                        network_policy_verified=network_policy_verified)
