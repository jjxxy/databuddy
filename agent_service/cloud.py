"""One-instance cloud entrypoint: persistent API + worker, shared lifecycle."""
import logging,os,signal,subprocess,sys,time
from pathlib import Path
from .process_control import spawn,stop as stop_process

log=logging.getLogger('atlas.cloud')

def prepare():
    if len(os.getenv('APP_TOKEN',''))<16:raise ValueError('APP_TOKEN must have at least 16 characters')
    port=int(os.getenv('PORT','8000'))
    if not 1<=port<=65535:raise ValueError('PORT must be between 1 and 65535')
    # Host-mounted 0600 files may only be readable before the one-time UID
    # switch. Neither provisioning mechanism is inherited by child processes.
    initial_model=os.environ.pop('ATLAS_INITIAL_MODEL','')
    model_path=os.environ.pop('ATLAS_LLM_CONFIG_FILE','')
    preset_path=os.environ.pop('ATLAS_EXTENSION_PRESETS_FILE','')
    if initial_model and model_path:raise ValueError('Use one model provisioning method')
    from .network_policy import mode
    mode()
    local=bool(os.getenv('ATLAS_LOCAL_WORKSPACE'))
    if local!=(os.getenv('ATLAS_LOCAL_MODE')=='1'):raise ValueError('Local workspace requires both ATLAS_LOCAL_MODE=1 and ATLAS_LOCAL_WORKSPACE')
    if local and all(os.getenv(key) for key in ('ATLAS_S3_ENDPOINT','ATLAS_S3_BUCKET','ATLAS_S3_ACCESS_KEY_ID','ATLAS_S3_SECRET_ACCESS_KEY','ATLAS_BACKUP_KEY')):
        raise ValueError('本地文件夹模式不支持旧格式远程备份；请停止服务后备份完整工作文件夹和部署配置')
    dropping=os.getenv('ATLAS_DROP_PRIVILEGES')=='1'
    uid=int(os.getenv('ATLAS_RUN_UID','10001')) if local else 10001
    gid=int(os.getenv('ATLAS_RUN_GID','10001')) if local else 10001
    if not 1<=uid<=2147483647 or not 1<=gid<=2147483647:raise ValueError('Local workspace UID/GID must be non-root positive IDs')
    if local:
        from .local_storage import prepare_storage
        storage_uid=None if os.name=='nt' else uid if dropping and os.geteuid()==0 else os.getuid()
        storage_gid=None if os.name=='nt' else gid if dropping and os.geteuid()==0 else os.getgid()
        data=prepare_storage(storage_uid,storage_gid)
    else:data=Path(os.getenv('DATA_DIR','/data')).resolve()
    from .preset_configuration import load_presets_file
    preset_configuration=load_presets_file(preset_path) if preset_path else None
    from .quickstart_model import load_configuration
    desktop=os.getenv('ATLAS_DESKTOP_MODE')=='1'
    try:
        model_configuration=load_configuration(model_path,container=os.getenv('ATLAS_QUICKSTART_CONTAINER')=='1') if model_path else None
        if desktop and model_configuration:
            entries=model_configuration.get('models',[model_configuration])
            if any('YOUR_' in e.get('model','') or 'YOUR_' in e.get('api_key','') or 'your-llm.example.com' in e.get('base_url','') for e in entries):
                raise ValueError('placeholder model configuration')
    except (ValueError,OSError):
        if not desktop:raise
        model_configuration=None
        os.environ['ATLAS_DESKTOP_MODEL_DISABLED']='1'
        os.environ['ATLAS_DESKTOP_MODEL_NOTICE']='云端模型不可用，本地能力可用。请由管理员检查模型配置。'
        log.warning('Desktop model configuration unavailable; local service remains available')
    if os.getenv('RAILWAY_ENVIRONMENT_ID'):
        mount=os.getenv('RAILWAY_VOLUME_MOUNT_PATH')
        if not mount or not data.is_relative_to(Path(mount).resolve()):
            raise ValueError('Railway needs a persistent volume mounted at DATA_DIR')
    data.mkdir(parents=True,exist_ok=True)
    os.environ['DATA_DIR']=str(data)
    os.environ['SCHEDULER_MODE']='worker'
    os.environ['AGENT_ORCHESTRATION_MODE']='separate'
    os.environ.setdefault('APP_TIMEZONE','Asia/Shanghai')
    os.environ.setdefault('DATABASE_URL','sqlite:///'+str(data/'metadata.sqlite'))
    if os.getenv('ATLAS_DROP_PRIVILEGES')=='1':
        if not hasattr(os,'geteuid'):raise ValueError('Cloud privilege dropping requires Linux')
        if os.geteuid()==0:
            # Only Atlas's own mounted directory is modified; unchanged volumes avoid a recursive walk.
            if not local and (data.stat().st_uid!=uid or data.stat().st_gid!=gid):
                os.chown(data,uid,gid)
                for root,dirs,files in os.walk(data,followlinks=False):
                    for name in dirs+files:
                        path=Path(root)/name
                        if not path.is_symlink():os.chown(path,uid,gid)
            os.setgroups([]);os.setgid(gid);os.setuid(uid)
            # The new owner can set its own directory mode without CAP_FOWNER.
            data.chmod(0o700)
            # A root-owned HOME must not survive the UID switch. Path.home() then uses
            # the agent account from passwd, including for dbt's user_settings.yml.
            os.environ.pop('HOME',None)
            if local:
                local_home=data/'home';local_home.mkdir(mode=0o700,exist_ok=True)
                os.environ['HOME']=str(local_home)
    from .migration import maybe_migrate
    migrated=maybe_migrate()
    if migrated:log.info('Metadata migration verified: %s',migrated)
    # Initialize all tables once before concurrent imports in the service processes.
    from . import store,jobs,audit,activity
    # One-time owner provisioning. Never replace an existing model connection
    # on restart, and never pass the provisioning secret to child processes.
    if model_configuration is not None and 'models' in model_configuration:
        from .model_catalog import provision
        from .quickstart_model import allow_model_host
        for choice in model_configuration['models']:allow_model_host(choice['base_url'])
        if desktop:provision(model_configuration,verify=False)
        else:provision(model_configuration)
        log.info('Owner model catalog encrypted; connection validation %s','deferred until use' if desktop else 'passed')
    elif initial_model or model_configuration is not None:
        try:existing_provider=store.get('system','model-provider')
        except KeyError:
            import json
            from .provider import ProviderInput,configure
            from .quickstart_model import allow_model_host,normalize_base_url
            value=ProviderInput.model_validate(model_configuration if model_configuration is not None else json.loads(initial_model),strict=True)
            if os.getenv('ATLAS_OPENCODE_RUNTIME')=='1' and value.engine=='atlas':value.tool_protocol='native'
            value.base_url=normalize_base_url(value.base_url,container=os.getenv('ATLAS_QUICKSTART_CONTAINER')=='1')
            allow_model_host(value.base_url)
            if desktop:
                from .model_catalog import provision
                provision(value.model_dump(mode='json') | {'api_key':value.api_key.get_secret_value()},verify=False)
            else:configure(value)
            log.info('Initial model connection verified and encrypted')
        else:
            # A new container does not inherit the prior process's host grant.
            # Restore only the active owner-selected endpoint, not a changed
            # bootstrap file which must never override a later UI setting.
            from .quickstart_model import allow_model_host
            allow_model_host(store.unseal(existing_provider['secret_config'])['base_url'])
    from .extensions import provision_presets
    provision_presets(preset_configuration)
    store.engine.dispose()
    return port

def main():
    logging.basicConfig(level=logging.INFO,format='%(name)s: %(message)s')
    try:port=prepare()
    except Exception as exc:
        log.error('Cloud configuration or storage initialization failed: %s errno=%s',type(exc).__name__,getattr(exc,'errno',None))
        return 2
    stopping=False
    def stop(signum,frame):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    if os.getenv('ATLAS_SUPERVISOR_STDIN')=='1':
        import threading
        def commands():
            for line in sys.stdin:
                if line.strip()=='stop':stop(None,None);return
            stop(None,None)
        threading.Thread(target=commands,daemon=True).start()
    services=[];result=0;verifier=None
    try:
        from . import opencode_runtime
        if opencode_runtime.is_enabled() and os.getenv('ATLAS_DESKTOP_MODEL_DISABLED')!='1':
            from .model_catalog import owner_configuration
            from .agent import Step
            configuration=owner_configuration()
            if configuration.get('models') or os.getenv('ATLAS_DESKTOP_MODE')!='1':
                try:
                    spec=opencode_runtime.prepare(configuration,Step.model_json_schema())
                    process=spawn(spec['command'],env=spec['env'],cwd=spec['cwd'])
                    services.append(('opencode',process));log.info('started opencode pid=%d',process.pid)
                    opencode_runtime.wait_ready(timeout=30)
                except Exception:
                    if os.getenv('ATLAS_DESKTOP_MODE')!='1':raise
                    for name,process in list(services):
                        if name=='opencode':stop_process(process);services.remove((name,process))
                    os.environ['ATLAS_DESKTOP_MODEL_DISABLED']='1'
                    os.environ['ATLAS_DESKTOP_MODEL_NOTICE']='云端模型不可用，本地能力可用。模型运行服务未就绪，请检查日志。'
                    log.warning('Desktop model runtime unavailable; local service remains available')
        commands=[('api',[sys.executable,'-m','uvicorn','agent_service.api:app','--host',os.getenv('ATLAS_API_HOST','0.0.0.0'),'--port',str(port)]),('worker',[sys.executable,'-m','agent_service.worker']),('agent',[sys.executable,'-m','agent_service.orchestrator'])]
        from .backup_cloud import configured
        if configured():commands.append(('backup',[sys.executable,'-m','agent_service.backup_service']))
        for name,args in commands:
            process=spawn(args)
            services.append((name,process));log.info('started %s pid=%d',name,process.pid)
        if os.getenv('ATLAS_CLOUD_VERIFY')=='1':
            verifier=spawn([sys.executable,'-m','agent_service.cloud_accept'])
        while not stopping:
            if verifier and verifier.poll() is not None:
                if verifier.returncode:
                    log.error('Trial acceptance failed; restarting the instance');result=1;stopping=True
                verifier=None
            for name,process in list(services):
                if process.poll() is not None:
                    if name=='opencode' and os.getenv('ATLAS_DESKTOP_MODE')=='1':
                        process.wait();services.remove((name,process))
                        log.error('Model runtime exited; local API remains available and model requests will fail explicitly')
                        continue
                    log.error('%s exited; shutting down the instance for a clean restart',name)
                    result=1;stopping=True;break
            if not stopping:time.sleep(.25)
    except Exception:
        log.exception('Cloud supervisor failed');result=1
    finally:
        if verifier and verifier.poll() is None:
            stop_process(verifier,timeout=3)
        # Linux parent-death protection also terminates any active collector/dbt descendant.
        for _,process in reversed(services):
            if process.poll() is None:
                process.terminate()
        deadline=time.monotonic()+8
        for _,process in services:
            try:process.wait(timeout=max(.1,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    return result

if __name__=='__main__':raise SystemExit(main())
