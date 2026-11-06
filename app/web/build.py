"""Build sem Node obrigatório, banco, segredos, pesquisas ou migrações implícitas."""
import hashlib
import json
from pathlib import Path
import tomllib

from app.web.config import ROOT


def main():
    static = ROOT / 'app/web/static'
    expected = ['index.html', 'tokens.css', 'styles.css', 'app.js', 'core.mjs', 'favicon.ico',
                'logos/dazum-wordmark-orange.svg', 'logos/dazum-wordmark-cream.svg',
                'logos/dazum-square-orange.svg', 'logos/dazum-square-cream.svg',
                'fonts/Poppins-Light.ttf', 'fonts/Poppins-Regular.ttf', 'fonts/Poppins-Medium.ttf',
                'fonts/Poppins-SemiBold.ttf', 'fonts/Poppins-Bold.ttf', 'fonts/OFL.txt']
    manifest = {name: hashlib.sha256((static / name).read_bytes()).hexdigest() for name in expected}
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    assert project['tool']['vercel']['entrypoint'] == 'app.web.api:app'
    config = json.loads((ROOT / 'vercel.json').read_text(encoding='utf-8'))
    assert config['functions']['app/web/api.py']['maxDuration'] == 30
    assert config['git']['deploymentEnabled'] is False
    html = (static / 'index.html').read_text(encoding='utf-8')
    assert all(f'/assets/{name}' in html for name in ('tokens.css', 'styles.css', 'app.js', 'favicon.ico'))
    for name in ['index.html', 'styles.css', 'app.js']:
        source = (static / name).read_text(encoding='utf-8')
        assert 'serper' not in source.lower() and 'api_key' not in source.lower()
    assert 'prefers-reduced-motion' in (static / 'styles.css').read_text(encoding='utf-8')
    target = ROOT / 'dist'
    target.mkdir(exist_ok=True)
    (target / 'web-manifest.json').write_text(json.dumps(dict(entrypoint='app.web.api:app', assets=manifest,
        database_migrations=False, external_queries=0), indent=2), encoding='utf-8')
    print(f'Build estático validado: {len(expected)} assets, entrypoint ASGI e configuração Vercel. Sem banco ou rede.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
