"""Generate deployment-only secrets. Never commit .env or data/."""
import secrets
from pathlib import Path
path=Path('.env')
if path.exists():
    print('.env 已存在，未覆盖。访问口令在 APP_TOKEN。')
else:
    token=secrets.token_urlsafe(32);password=secrets.token_urlsafe(24)
    path.write_text(f'APP_TOKEN={token}\nPOSTGRES_PASSWORD={password}\nAPP_TIMEZONE=Asia/Shanghai\nLLM_BASE_URL=\nLLM_API_KEY=\nLLM_MODEL=\nAPI_ALLOWED_HOSTS=\n')
    path.chmod(0o600)
    print('已生成 .env；访问口令见 APP_TOKEN。不要分享或提交此文件。')
