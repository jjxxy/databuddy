from agent_service.store import put,patch,get,delete

def test_schedule_and_publication_updates_preserve_each_other():
    p=put('pipeline',{'cron':None,'published_run':None,'name':'独立字段更新'})
    patch('pipeline',p['id'],published_run='good-run',status='published')
    patch('pipeline',p['id'],cron='0 9 * * *',next_due='2099-01-01T00:00:00+00:00')
    r=get('pipeline',p['id'])
    assert r['published_run']=='good-run' and r['cron']=='0 9 * * *'
    patch('pipeline',p['id'],published_run='next-run')
    assert get('pipeline',p['id'])['next_due']=='2099-01-01T00:00:00+00:00'

    delete('pipeline',p['id'])
