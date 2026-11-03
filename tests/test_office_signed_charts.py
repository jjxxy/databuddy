"""Signed financial charts use verifiable shapes, never unsafe Office axes."""
from decimal import Decimal
from pathlib import Path
import os
import shutil
import subprocess

import pytest
from pptx import Presentation
from pptx.util import Inches
from agent_service import office


def spec(points):
    return office.Spec.model_validate({'kind':'presentation','title':'Quarterly','sections':[
        {'heading':'Margin','chart':{'title':'Change','unit':'CNY','points':points}}]})


def build(tmp_path,points,version=office.RENDERER):
    value=spec(points);path=tmp_path/'signed.pptx'
    office._build_file(value,path,[],renderer_version=version)
    office._verify_structure(path,'pptx',value)
    return value,path,Presentation(path)


def test_mixed_sign_shapes_have_exact_values_direction_and_scaled_width(tmp_path):
    original,path,deck=build(tmp_path,[{'label':'East','value':'-100.00'},
        {'label':'West','value':'60.00'},{'label':'Zero','value':'0.00'}])
    shapes={s.name:s for s in deck.slides[-1].shapes};axis=shapes['nexus:signed-axis:0']
    negative=shapes['nexus:signed-bar:0:0'];positive=shapes['nexus:signed-bar:0:1']
    assert not any(s.has_chart for s in deck.slides[-1].shapes)
    assert negative.left+negative.width==axis.left
    assert positive.left==axis.left
    assert Decimal(positive.width)/Decimal(negative.width)==Decimal('0.6')
    assert 'nexus:signed-bar:0:2' not in shapes
    assert shapes['nexus:chart-value:0:0'].text=='-100.00'
    assert shapes['nexus:chart-value:0:1'].text=='60.00'
    assert '修改数值后需重新生成' in shapes['nexus:signed-note:0'].text
    # A moved bar must fail integrity verification, not silently reverse sign.
    negative.left=axis.left;deck.save(path)
    with pytest.raises(ValueError,match='完整性验证失败'):office._verify_structure(path,'pptx',original)


def test_signed_values_all_negative_and_exact_decimal(tmp_path):
    _,_,deck=build(tmp_path,[{'label':'A','value':'-0.1'}, {'label':'B','value':'-0.3'}])
    shapes={s.name:s for s in deck.slides[-1].shapes};axis=shapes['nexus:signed-axis:0']
    assert shapes['nexus:signed-bar:0:0'].left+shapes['nexus:signed-bar:0:0'].width==axis.left
    assert shapes['nexus:signed-bar:0:1'].left+shapes['nexus:signed-bar:0:1'].width==axis.left
    assert shapes['nexus:chart-value:0:0'].text=='-0.1' and shapes['nexus:chart-value:0:1'].text=='-0.3'


def test_tiny_signed_values_use_exact_complete_table_instead_of_exaggerated_bar(tmp_path):
    value,path,deck=build(tmp_path,[{'label':'A','value':'-0.0000001'}, {'label':'B','value':'1000.00'}])
    shapes={s.name:s for s in deck.slides[-1].shapes}
    table=shapes['nexus:signed-table:0'].table
    assert [[table.cell(r,c).text for c in range(2)] for r in range(3)]==[
        ['类别','原始数值 · CNY'],['A','-0.0000001'],['B','1000.00']]
    assert not any(s.name.startswith('nexus:signed-bar:') for s in deck.slides[-1].shapes)
    assert '完整原值表格展示' in shapes['nexus:signed-fallback:0'].text
    table.cell(1,1).text='0.0000001';deck.save(path)
    with pytest.raises(ValueError,match='完整性验证失败'):office._verify_structure(path,'pptx',value)


@pytest.mark.parametrize('version',['office-v5','office-v6'])
def test_frozen_older_versions_do_not_silently_gain_new_signed_behavior(tmp_path,version):
    value=spec([{'label':'A','value':'-1'}])
    with pytest.raises(ValueError,match='旧版'):
        office._build_file(value,tmp_path/(version+'.pptx'),[],renderer_version=version)


@pytest.mark.skipif(not shutil.which('soffice') or not shutil.which('pdftoppm'),reason='needs LibreOffice and Poppler')
def test_libreoffice_real_pdf_draws_negative_left_positive_right(tmp_path):
    """Inspect rendered pixels, not only PPTX XML or python-pptx metadata."""
    from PIL import Image
    _,path,_=build(tmp_path,[{'label':'East','value':'-100.00'}, {'label':'West','value':'60.00'}, {'label':'Zero','value':'0.00'}])
    env=dict(os.environ)
    subprocess.run(['soffice','--headless','--convert-to','pdf','--outdir',str(tmp_path),str(path)],
                   check=True,capture_output=True,timeout=60,env=env)
    prefix=tmp_path/'page'
    subprocess.run(['pdftoppm','-f','2','-l','2','-singlefile','-r','96','-png',str(tmp_path/'signed.pdf'),str(prefix)],
                   check=True,capture_output=True,timeout=30)
    image=Image.open(str(prefix)+'.png').convert('RGB');axis=round(5.15*96)
    # Exact fill interior pixels in the two known row bands; both colors must
    # be visible and on the mathematically correct side of the zero line.
    def points(color,ylo,yhi):
        return [(x,y) for y in range(ylo,yhi) for x in range(220,760) if image.getpixel((x,y))==color]
    red=points((185,78,67),220,255);green=points((42,121,96),265,300)
    assert len(red)>500 and len(green)>300
    assert min(x for x,_ in red)<axis-100 and max(x for x,_ in red)<=axis
    assert min(x for x,_ in green)>=axis-1 and max(x for x,_ in green)>axis+50
