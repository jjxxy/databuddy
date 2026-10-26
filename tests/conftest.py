import os
import tempfile
os.environ['DATA_DIR']=tempfile.mkdtemp(prefix='atlas-tests-')
os.environ['APP_TOKEN']='test-only-access-token-123456789'
os.environ['LLM_BASE_URL']=''
os.environ['LLM_MODEL']=''
os.environ['DLT_TELEMETRY']='false'
os.environ['DO_NOT_TRACK']='1'
