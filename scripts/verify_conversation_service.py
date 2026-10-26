"""Exercise the conversation against a separate real API/worker and restart it."""
import json,os,secrets,socket,subprocess,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    with tempfile.TemporaryDirectory(prefix='atlas-chat-service-') as folder:
        folder=Path(folder)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        env=dict(os.environ,DATA_DIR=str(folder/'data'),DATABASE_URL='sqlite:///'+str(folder/'data/metadata.sqlite'),APP_TOKEN=secrets.token_urlsafe(32),PORT=str(port),PYTHONPATH=str(ROOT),LLM_BASE_URL='',LLM_MODEL='',ATLAS_CLOUD_VERIFY='1',SCHEDULER_MODE='worker')
        with (folder/'service.log').open('w') as log:
            for restart in (False,True):
                process=subprocess.Popen([sys.executable,'-m','agent_service.cloud'],cwd=ROOT,env=env,stdout=log,stderr=log)
                try:
                    deadline=time.monotonic()+240
                    expected='ATLAS_AGENT_CONTRACTS_RESTART_VERIFIED' if restart else 'ATLAS_AGENT_CONTRACTS_ACCEPTANCE'
                    while time.monotonic()<deadline:
                        output=(folder/'service.log').read_text()
                        if expected in output:
                            report=next(line for line in output.splitlines() if line.startswith(expected+' '))
                            print(report,flush=True);break
                        if process.poll() is not None:raise RuntimeError('service exited')
                        time.sleep(.5)
                    else:raise TimeoutError('conversation service verification timed out')
                except Exception:
                    print((folder/'service.log').read_text()[-6000:]);raise
                finally:
                    if process.poll() is None:process.terminate()
                    process.wait(timeout=15)
                    assert process.returncode==0

if __name__=='__main__':main()
