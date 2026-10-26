"""Standard-library deployment helper. No dotenv evaluation or app imports.

The Docker-only wrappers run setup/info using the base Python image. Host
Python is optional. This tool never deletes a volume or changes a saved token.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        raise ValueError('.env 文件无效或过大')
    result = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', key):
            raise ValueError('.env 包含无效配置；此文件不会作为脚本执行')
        if key in result:
            raise ValueError('.env 存在重复配置项，请保留每项一个值')
        value = value.strip()
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("\\'", "'").replace('\\\\', '\\')
        elif value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
            value = value.replace('\\"', '"').replace('\\\\', '\\')
        elif ' #' in value:
            value = value.split(' #', 1)[0].rstrip()
        if any(ord(char) < 32 for char in value):
            raise ValueError('.env 配置不能包含控制字符')
        result[key] = value
    return result


def _atomic(path: Path, content: str):
    if path.is_symlink():
        raise ValueError('配置不能使用符号链接')
    fd, name = tempfile.mkstemp(prefix='.atlas-setup-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(content);stream.flush();os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _validate_runtime(values):
    token = values.get('APP_TOKEN', '')
    if len(token) < 16:
        raise ValueError('已有 APP_TOKEN 太短；请使用至少16字符的访问口令，系统不会擅自覆盖')
    if values.get('ATLAS_OPERATOR_TOKEN') and len(values['ATLAS_OPERATOR_TOKEN'])<16:
        raise ValueError('已有 ATLAS_OPERATOR_TOKEN 太短；请使用至少16字符的独立后台管理口令')
    if values.get('ATLAS_OPERATOR_TOKEN')==token:
        raise ValueError('后台管理口令必须与工作空间访问口令不同')
    port = values.get('ATLAS_HTTP_PORT', '8000')
    if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('ATLAS_HTTP_PORT 必须是1至65535的端口')
    if values.get('ATLAS_BIND_ADDRESS', '127.0.0.1') not in ('127.0.0.1', '0.0.0.0'):
        raise ValueError('ATLAS_BIND_ADDRESS 仅支持127.0.0.1或0.0.0.0')
    return values


def configuration(directory: Path):
    # __init__ is empty and the loader is intentionally stdlib-only. This must
    # not import config.py before the deployment environment is loaded.
    from agent_service.quickstart_model import load_configuration
    value = load_configuration(directory / 'llm.json')
    for model in value.get('models', [value]):
        if (urlsplit(model['base_url']).hostname == 'your-llm.example.com' or
                model['model'] == 'YOUR_MODEL' or model.get('api_key') == 'YOUR_API_KEY'):
            raise ValueError('请在 llm.json 填写 LLM 接口地址、模型名和 API Key；目录中的每个模型都需要配置')
    return value


def setup(directory: Path):
    directory = directory.resolve()
    if not directory.is_dir():
        raise ValueError('部署目录不存在')
    path = directory / '.env';values = read_env(path)
    model_path = directory / 'llm.json'
    if not model_path.exists():
        # Preserve the earlier three-variable deployment format when migrating.
        if values.get('LLM_BASE_URL') and values.get('LLM_MODEL'):
            model = {'base_url': values['LLM_BASE_URL'], 'model': values['LLM_MODEL'],
                     'api_key': values.get('LLM_API_KEY', ''), 'share_data': False,
                     'tool_protocol': 'json'}
            _atomic(model_path, json.dumps(model, ensure_ascii=False, indent=2) + '\n')
        else:
            _atomic(model_path, (ROOT / 'llm.example.json').read_text(encoding='utf-8'))
            raise ValueError('已生成 llm.json。请只填写接口地址、模型名和 API Key，再次启动')
    model = configuration(directory)
    defaults = {'APP_TOKEN': secrets.token_urlsafe(32), 'ATLAS_OPERATOR_TOKEN':secrets.token_urlsafe(32), 'APP_TIMEZONE': 'Asia/Shanghai',
                'ATLAS_HTTP_PORT': '8000', 'ATLAS_BIND_ADDRESS': '127.0.0.1'}
    missing = {key: value for key, value in defaults.items() if not values.get(key)}
    _validate_runtime({**values, **missing})
    if missing:
        original = path.read_text(encoding='utf-8-sig') if path.exists() else ''
        # Empty definitions must be replaced rather than creating duplicate keys.
        lines = original.splitlines()
        lines = [line for line in lines if not any(
            re.match(r'^\s*(?:export\s+)?' + re.escape(key) + r'\s*=', line) for key in missing)]
        content = '\n'.join(lines).rstrip('\n')
        if content:
            content += '\n'
        content += ''.join(f'{key}={value}\n' for key, value in missing.items())
        _atomic(path, content)
    choices = model.get('models', [model])
    return {'configured': True, 'model': choices[0]['model'], 'share_data': any(m.get('share_data',False) for m in choices),
            'secrets_generated': bool(missing), 'scope': 'single_owner_deployment'}


def info(directory: Path):
    values = _validate_runtime(read_env(directory / '.env'))
    print('工作空间：http://localhost:' + values.get('ATLAS_HTTP_PORT', '8000'))
    print('访问口令：' + values['APP_TOKEN'])
    print('请保留整个部署目录与 Docker 数据卷。停止不会删除数据。')


def compose(directory: Path, arguments):
    if not shutil.which('docker'):
        raise ValueError('请先安装并启动 Docker Desktop 或 Docker Engine 与 Compose v2')
    command = ['docker', 'compose', '--env-file', str(directory / '.env'),
               '-f', str(directory / 'compose.quickstart.yaml'), *arguments]
    return subprocess.run(command, cwd=directory, check=False).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('setup', 'check', 'info', 'up', 'down', 'status', 'logs', 'open'))
    parser.add_argument('--directory', type=Path, default=ROOT)
    args = parser.parse_args(argv);directory = args.directory.resolve()
    try:
        if args.command == 'setup':
            result = setup(directory)
            print('配置已检查。' + ('上传资料将按任务需要发送给你配置的 LLM。' if result['share_data'] else
                  '模型内容读取未开启，由部署管理员在后台配置中启用。'))
            return 0
        if args.command == 'check':
            configuration(directory);_validate_runtime(read_env(directory / '.env'))
            print('LLM 与运行配置检查通过；尚未执行模型连接或 Docker 启动。');return 0
        if args.command == 'info':
            info(directory);return 0
        if args.command == 'open':
            values = _validate_runtime(read_env(directory / '.env'))
            webbrowser.open('http://localhost:' + values.get('ATLAS_HTTP_PORT', '8000'));return 0
        if args.command == 'up':
            setup(directory)
            result = compose(directory, ['up', '-d', '--build', '--wait', '--wait-timeout', '180'])
            if result == 0:
                info(directory)
            else:
                print('启动未就绪，请运行 start.sh logs 或 start.cmd logs 查看日志。', file=sys.stderr)
            return result
        return compose(directory, {'down': ['down'], 'status': ['ps'],
                                   'logs': ['logs', '--tail', '150', 'atlas']}[args.command])
    except (ValueError, OSError, json.JSONDecodeError):
        # Never include JSON/URL/key values in setup error diagnostics.
        error = sys.exception()
        message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else '配置文件无法读取，请检查 JSON 格式和文件权限'
        print(message, file=sys.stderr);return 2


if __name__ == '__main__':
    raise SystemExit(main())
