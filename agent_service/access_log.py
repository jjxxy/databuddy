"""Redact short-lived ONLYOFFICE bearer URLs from application access logs.

The token is bound to a trusted source IP, expires quickly and is single-use,
which does not make it safe to retain in logs. This filter applies to the
uvicorn.access logger only; external proxies must redact independently.
"""
import logging
import re

_PATH = re.compile(r'(?i)(/api/onlyoffice/file(?:/|%2f))[^\s"\'\\]+')
_SENSITIVE_KEYS = frozenset({'token','ticket','access_token','bearer','authorization'})


def _redact(value, *, sensitive=False, key=None):
    if sensitive and isinstance(key,str) and key.lower() in _SENSITIVE_KEYS:
        return '[REDACTED]'
    if isinstance(value,str):
        return _PATH.sub(r'\1[REDACTED]',value)
    if isinstance(value,dict):
        return {k:_redact(v,sensitive=sensitive,key=k) for k,v in value.items()}
    if isinstance(value,tuple):
        return tuple(_redact(v,sensitive=sensitive) for v in value)
    if isinstance(value,list):
        return [_redact(v,sensitive=sensitive) for v in value]
    return value


class OnlyOfficeTicketFilter(logging.Filter):
    def filter(self,record):
        # Uvicorn's default access logger puts the complete path in a tuple;
        # JSON/structured formatters may put it in msg, args or extra fields.
        # Rebuild containers instead of mutating caller-owned request data.
        try:
            sensitive='/api/onlyoffice/file' in repr(record.__dict__).lower()
            record.msg=_redact(record.msg,sensitive=sensitive)
            record.args=_redact(record.args,sensitive=sensitive)
            for key,value in list(record.__dict__.items()):
                if key not in ('msg','args'):
                    record.__dict__[key]=_redact(value,sensitive=sensitive,key=key)
        except Exception:
            # If an untrusted structured record cannot safely be inspected,
            # suppress this access log rather than risk emitting a bearer.
            return False
        return True


def install():
    logger=logging.getLogger('uvicorn.access')
    if not any(isinstance(value,OnlyOfficeTicketFilter) for value in logger.filters):
        logger.addFilter(OnlyOfficeTicketFilter())
