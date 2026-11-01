"""Endpoint-specific options for Atlas's bounded model requests.

DeepSeek's default thinking mode rejects the required tool choice used by Atlas.
Disable it for the official DeepSeek endpoint, including short connection probes
whose output budget is reserved for the answer or tool arguments. Other model
services retain their existing request body.

https://api-docs.deepseek.com/api/create-chat-completion/
"""
import json,os
from urllib.parse import urlsplit


def managed_model_limits(base_url: str, model: str, *, context_window_tokens=None, max_output_tokens=None) -> dict:
    """Known upstream capacities, not an Atlas task budget or startup probe.

    DeepSeek /models and its official API reference were checked 2026-10-24.
    Unknown endpoints/models retain the native runtime's metadata discovery.
    """
    for value in (context_window_tokens,max_output_tokens):
        if value is not None and (type(value) is not int or value<=0):
            raise ValueError('模型窗口和输出上限须为正整数 token')
    if context_window_tokens is not None and max_output_tokens is not None and max_output_tokens>=context_window_tokens:
        raise ValueError('模型输出上限必须小于上下文窗口')
    try:
        endpoint=urlsplit(base_url)
        official=(endpoint.scheme=='https' and endpoint.hostname=='api.deepseek.com'
                  and endpoint.port in (None,443))
    except ValueError:official=False
    limits={}
    if official and model=='deepseek-v4-pro':
        limits={'context':1048576,'output':393216}
    if context_window_tokens is not None:
        limits['context']=context_window_tokens
        # Avoid applying an inherited output limit larger than an explicitly
        # overridden window. A small fallback is a maximum, not a reservation
        # of the entire window for output.
        if max_output_tokens is None and limits.get('output',3200)>=context_window_tokens:
            limits['output']=min(3200,max(1,context_window_tokens//4))
    if max_output_tokens is not None:limits['output']=max_output_tokens
    return limits


def response_token_allowance(value):
    return managed_model_limits(value.get('base_url',''),value.get('model',''),
        context_window_tokens=value.get('context_window_tokens'),max_output_tokens=value.get('max_output_tokens')).get('output',3200)


def configured_input_character_limit(model, *, compaction=False):
    """Only an explicit operator cap limits transport characters; unset=None."""
    limits=json.loads(os.getenv('ATLAS_OPENCODE_MODEL_INPUT_CHAR_LIMITS','{}'))
    if not isinstance(limits,dict) or any(not isinstance(k,str) or type(v) is not int or v<=0 for k,v in limits.items()):
        raise ValueError('模型上下文字符上限配置无效')
    cap=limits.get(model,limits.get('*'))
    explicit=os.getenv('ATLAS_OPENCODE_COMPACTION_MAX_INPUT_CHARACTERS')
    if compaction and explicit is not None:
        try:requested=int(explicit)
        except ValueError:raise ValueError('压缩上下文字符上限配置无效') from None
        if requested<=0:raise ValueError('压缩上下文字符上限须为正整数')
        cap=requested if cap is None else min(cap,requested)
    return cap


def bounded_request_options(base_url: str, model: str) -> dict:
    try:
        endpoint = urlsplit(base_url)
        official = (endpoint.scheme == 'https' and endpoint.hostname == 'api.deepseek.com'
                    and endpoint.port in (None, 443))
    except ValueError:
        return {}
    if official and model.startswith('deepseek-'):
        return {'thinking': {'type': 'disabled'}}
    return {}
