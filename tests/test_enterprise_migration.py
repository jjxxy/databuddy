import os,json,subprocess,sys
from pathlib import Path
import pytest
from sqlalchemy import text
from agent_service.store import engine,uid

@pytest.mark.skipif(engine.dialect.name!='postgresql',reason='requires real PostgreSQL')
def test_atomic_sqlite_migration_keeps_key_audit_rows_and_reuses_marker(tmp_path):
    from sqlalchemy.engine import make_url
    schema='verify_'+uid();base=tmp_path/'source';source_url='sqlite:///'+str(base/'metadata.sqlite')
    setup='''import json
from agent_service.conversation import create_project,ProjectInput
from agent_service.store import put,seal
from agent_service.audit import record
p=create_project(ProjectInput(name="迁移保留的项目",request_id="migration-fixture-project"))
put("system",{"secret_config":seal({"fixture_secret":"retained-private-value"})},"migration-fixture-private")
record("fixture.migration","test");print(json.dumps({"id":p["id"]}))'''
    env=dict(os.environ,DATA_DIR=str(base),DATABASE_URL=source_url);pid=json.loads(subprocess.check_output([sys.executable,'-c',setup],env=env,text=True).splitlines()[-1])['id']
    original=(base/'connector.key').read_bytes()
    with engine.begin() as c:c.execute(text('CREATE SCHEMA '+schema))
    try:
        url=make_url(str(engine.url.render_as_string(hide_password=False)));query=dict(url.query);query['options']='-c search_path='+schema;url=url.set(query=query)
        env.update(DATABASE_URL=url.render_as_string(hide_password=False),ATLAS_MIGRATE_SQLITE='1')
        script='''import json,sys
from agent_service.migration import maybe_migrate
r=maybe_migrate();assert r["ok"]
from agent_service.store import get,unseal
from agent_service.audit import verify
assert get("project",sys.argv[1])["name"]=="迁移保留的项目"
assert unseal(get("system","migration-fixture-private")["secret_config"])["fixture_secret"]=="retained-private-value"
assert verify()["ok"] and maybe_migrate()["reused"]
print("MIGRATION_VERIFIED")'''
        assert 'MIGRATION_VERIFIED' in subprocess.check_output([sys.executable,'-c',script,pid],env=env,text=True)
        assert (base/'connector.key').read_bytes()==original and (base/'metadata.sqlite').is_file() and (base/'migrations/pre-postgres.sqlite').is_file()
    finally:
        with engine.begin() as c:c.execute(text('DROP SCHEMA '+schema+' CASCADE'))
