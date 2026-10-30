"""Run the desktop backend from source, using installed development tools.

Packaged desktop builds retain their explicit bundled-binary requirement.
This entry point never removes project data or resets a saved credential.
"""
import argparse
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent


def configure_git(environment=None):
    environment = os.environ if environment is None else environment
    selected = environment.get('ATLAS_GIT_BINARY')
    if not selected:
        selected = shutil.which('git', path=environment.get('PATH', os.defpath))
        if not selected:raise ValueError('未找到 Git；请安装 Git 或设置 ATLAS_GIT_BINARY 为 git 可执行文件的绝对路径')
        selected = str(Path(selected).resolve())
    if not Path(selected).is_absolute() or not Path(selected).is_file():
        raise ValueError('ATLAS_GIT_BINARY 必须指向现有 Git 可执行文件的绝对路径')
    environment['ATLAS_GIT_BINARY'] = selected
    return selected


def configure_opencode(environment=None):
    """Honor explicit settings, then find OpenCode in PATH or the project bundle."""
    environment = os.environ if environment is None else environment
    selected = environment.get('ATLAS_OPENCODE_BINARY')
    if not selected:
        selected = shutil.which('opencode', path=environment.get('PATH', os.defpath))
        if not selected:
            bundled=ROOT/'runtime'/'opencode'/('opencode.exe' if os.name=='nt' else 'opencode')
            if bundled.is_file() and (os.name=='nt' or os.access(bundled,os.X_OK)):
                selected=str(bundled.resolve())
        if selected:environment['ATLAS_OPENCODE_BINARY']=str(Path(selected).resolve())
    environment.setdefault('ATLAS_OPENCODE_RUNTIME','1' if selected else '0')
    return environment['ATLAS_OPENCODE_RUNTIME']=='1'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', help='独立调试状态目录；不传时保留已有 DATA_DIR / DATABASE_URL')
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT', '8000')))
    parser.add_argument('--model-config', default=os.getenv('ATLAS_LLM_CONFIG_FILE'))
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:parser.error('端口必须在1至65535之间')
    try:
        configure_git()
        from agent_service.platform_files import assert_plain_path, private_directory
        from portable_runtime import credentials
        state = Path(args.state_dir or ROOT / '.atlas-debug').expanduser().absolute()
        assert_plain_path(state);state.mkdir(parents=True, exist_ok=True);private_directory(state)
        inherited_token = bool(os.environ.get('APP_TOKEN'))
        access = credentials(state)
        for key,value in access.items():os.environ.setdefault(key,value)
        if args.state_dir or not os.getenv('DATA_DIR'):os.environ['DATA_DIR'] = str(state / 'data')
        if args.state_dir or not os.getenv('DATABASE_URL'):
            os.environ['DATABASE_URL'] = 'sqlite:///' + str(Path(os.environ['DATA_DIR']).absolute() / 'metadata.sqlite')
        os.environ.update(ATLAS_DESKTOP_MODE='1',ATLAS_API_HOST='127.0.0.1',PORT=str(args.port),PYTHONUTF8='1')
        os.environ['PYTHONPATH'] = str(ROOT) + (os.pathsep + os.environ['PYTHONPATH'] if os.getenv('PYTHONPATH') else '')
        native=configure_opencode()
        model = Path(args.model_config).expanduser().absolute() if args.model_config else ROOT / 'llm.json'
        if args.model_config or model.is_file():os.environ['ATLAS_LLM_CONFIG_FILE'] = str(model)
        hint = '访问口令沿用 APP_TOKEN 环境变量' if inherited_token else f'调试凭据保存在 {state / "credentials.json"}'
        print(f'本地调试地址：http://127.0.0.1:{args.port}；{hint}；模型执行框架：'+('OpenCode（以启动结果为准）' if native else 'Atlas 兼容模式'),flush=True)
        from agent_service.cloud import main as run
        return run()
    except (OSError,ValueError) as error:
        parser.exit(2,str(error)+'\n')


if __name__ == '__main__':raise SystemExit(main())
