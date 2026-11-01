"""Bounded model HTTP transport timeouts.

The read timeout is an inactivity timeout between network reads, not a limit on
an entire model response. Thinking and streaming models may spend several
minutes before sending the first body byte, or between SSE events. Receiving a
heartbeat or a reasoning chunk resets httpx's inactivity timer.
"""
import math
import os

import httpx


def model_read_timeout():
    raw=os.getenv('ATLAS_MODEL_READ_TIMEOUT_SECONDS','900')
    try:value=float(raw)
    except (ValueError,TypeError):raise ValueError('模型读取超时配置无效') from None
    if not math.isfinite(value) or not 10<=value<=7200:
        raise ValueError('模型读取超时配置需在 10 到 7200 秒之间')
    return value


def model_http_timeout():
    return httpx.Timeout(connect=15.0,read=model_read_timeout(),write=60.0,pool=15.0)
