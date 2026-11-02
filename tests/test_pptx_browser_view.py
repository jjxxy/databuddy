import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from pptx import Presentation

from agent_service.api import app


def test_presentation_frame_disables_network():
    response = TestClient(app).get('/pptx-frame.html')
    assert response.status_code == 200
    policy = response.headers['content-security-policy']
    for directive in ("default-src 'none'", "connect-src 'none'", "frame-src 'none'", "form-action 'none'", "img-src data: blob:"):
        assert directive in policy


def test_presentation_renderer_handles_real_pptx(tmp_path):
    deck = Presentation()
    for title in ('财经月结汇报', '精确金额 118.15'):
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = title
    presentation = tmp_path / 'test.pptx'
    deck.save(presentation)
    root = Path(__file__).resolve().parents[1]
    subprocess.run(['node', str(root / 'tests/ui/pptx-sandbox.cjs'), str(presentation)], cwd=root, check=True, timeout=30)
