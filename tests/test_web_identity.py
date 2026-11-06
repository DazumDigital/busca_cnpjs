import hashlib
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from app.web.api import STATIC, create_app
from app.web.config import WebSettings
from app.web.results import corresponding_social

MANIFEST=json.loads((Path(__file__).parent/'fixtures/web_brand_manifest.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('name,expected',list(MANIFEST.items()))
def test_official_assets_are_unmodified_and_packaged(name,expected):
    path=STATIC/name if name.startswith('fonts/') or name=='favicon.ico' else STATIC/'logos'/name
    assert hashlib.sha256(path.read_bytes()).hexdigest()==expected
    if name.endswith('.svg'):
        tree=ET.fromstring(path.read_bytes())
        assert not any(e.tag.split('}')[-1] in {'script','foreignObject','image'} for e in tree.iter())


def test_official_color_font_and_motion_tokens():
    css=(STATIC/'tokens.css').read_text(encoding='utf-8').lower()
    for name,color in {'navy':'#0a1d3f','orange':'#ff5a1f','bone':'#f4f4f2','bone-deep':'#efefeb',
                       'white':'#ffffff','ink':'#17110e','body':'#191919','muted':'#5f6368','line':'#e4e4e0',
                       'official-orange':'#fe6b35','official-navy':'#002365'}.items():
        assert '--'+name+':'+color in css
    assert 'cubic-bezier(0.22,1,0.36,1)' in css
    assert css.count('@font-face')==5
    assert 'Poppins' in (STATIC/'index.html').read_text(encoding='utf-8')
    body=(STATIC/'styles.css').read_text(encoding='utf-8')
    assert 'prefers-reduced-motion' in body
    assert not re.search(r'#[0-9a-fA-F]{3,8}\b',body)


def test_assets_are_served_locally_without_remote_font_calls():
    with TestClient(create_app(WebSettings(origin='http://testserver'))) as client:
        for path in ['/favicon.ico','/assets/favicon.ico','/assets/logos/dazum-wordmark-orange.svg',
                     '/assets/logos/dazum-square-orange.svg','/assets/fonts/Poppins-Regular.ttf','/assets/tokens.css']:
            assert client.get(path).status_code==200
        html=client.get('/').text
        assert 'fonts.googleapis.com' not in html and 'fonts.gstatic.com' not in html
        assert 'brand-symbol' not in html


def test_unrelated_social_people_and_generic_posts_are_not_shown_as_profiles():
    company=dict(cnpj='00055627000140',legal_name='THP COMERCIO E REPRESENTACOES LTDA',trade_name='',city='Blumenau',state='')
    assert not corresponding_social('https://www.instagram.com/p/CPECzmPFAlh/',company)
    assert not corresponding_social('https://br.linkedin.com/in/jaquemagalhaes',company)
    assert corresponding_social('https://www.instagram.com/thpbrasil/',company)


def test_forms_never_use_get_and_stay_disabled_until_javascript_is_ready():
    from html.parser import HTMLParser
    class Forms(HTMLParser):
        def __init__(self):super().__init__();self.forms={};self.buttons={}
        def handle_starttag(self,tag,attrs):
            attrs=dict(attrs)
            if tag=='form':self.forms[attrs['id']]=attrs
            if tag=='button' and 'id' in attrs:self.buttons[attrs['id']]=attrs
    parsed=Forms();parsed.feed((STATIC/'index.html').read_text(encoding='utf-8'))
    for form,action,button in [('login-form','/api/login','login-button'),('search-form','/api/consultas','search-button')]:
        assert parsed.forms[form]['method'].lower()=='post'
        assert parsed.forms[form]['action']==action
        assert 'disabled' in parsed.buttons[button]
