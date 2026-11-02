from fastapi.testclient import TestClient
from agent_service.api import app

def test_docx_frame_security_policy_is_narrowly_scoped():
    client=TestClient(app)
    frame=client.get('/docx-frame.html')
    assert frame.status_code==200
    policy=frame.headers['content-security-policy']
    assert "default-src 'none'" in policy
    assert "connect-src 'none'" in policy
    assert "frame-src 'none'" in policy
    assert "form-action 'none'" in policy
    assert "frame-ancestors 'self'" in policy
    assert "img-src data: blob:" in policy
    assert "'unsafe-eval'" not in policy
    main=client.get('/')
    assert "frame-ancestors 'none'" in main.headers['content-security-policy']
