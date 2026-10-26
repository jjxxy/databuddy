import os
from pathlib import Path
os.environ.setdefault("RUNTIME__DLTHUB_TELEMETRY", "false")
os.environ.setdefault("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
DATA = Path(os.getenv('DATA_DIR', './data')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
DATA.chmod(0o700)
DATABASE_URL = os.getenv('DATABASE_URL', f'sqlite:///{DATA}/metadata.sqlite')
TOKEN = os.getenv('APP_TOKEN', '')
MAX_UPLOAD = int(os.getenv('MAX_UPLOAD_MB', '25')) * 1024 * 1024
MAX_ROWS = int(os.getenv('MAX_SOURCE_ROWS', '100000'))
TIMEZONE = os.getenv('APP_TIMEZONE', 'Asia/Shanghai')
LLM_BASE = os.getenv('LLM_BASE_URL', '')
LLM_KEY = os.getenv('LLM_API_KEY', '')
LLM_MODEL = os.getenv('LLM_MODEL', '')
