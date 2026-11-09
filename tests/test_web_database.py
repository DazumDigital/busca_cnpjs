import certifi
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from fastapi.testclient import TestClient
from app.web.config import WebSettings
from app.web.api import create_app
from app.web.diagnostics import database_diagnostic


def test_environment_keeps_password_and_channel_binding_with_portable_ca(monkeypatch):
    value = 'postgresql+psycopg://owner:a%40b%3Ac%2Fd@pool.example/db?sslmode=verify-full&channel_binding=require&sslrootcert=system'
    monkeypatch.setenv('WEB_DATABASE_URL', value)
    monkeypatch.setenv('WEB_ENV', 'production')
    monkeypatch.setenv('WEB_ORIGIN', 'https://site.example')
    parsed = make_url(WebSettings.from_env().database_url)
    assert parsed.password == 'a@b:c/d'
    assert parsed.query['sslmode'] == 'verify-full'
    assert parsed.query['sslrootcert'] == certifi.where()
    assert parsed.query['channel_binding'] == 'require'


@pytest.mark.parametrize('message,expected',[
    ('certificate verify failed password=private','DB_CERTIFICATE'),
    ('channel binding required password=private','DB_CHANNEL_BINDING'),
    ('password authentication failed for private','DB_AUTHENTICATION'),
    ('could not translate host private','DB_DNS'),
    ('connection timed out private','DB_TIMEOUT'),
    ('SSL connection failed private','DB_TLS'),
    ('unrecognized private','DB_OPERATIONAL'),
])
def test_database_diagnostic_has_no_sensitive_text(message, expected):
    assert database_diagnostic(RuntimeError(message)) == expected


def test_failed_connection_returns_503_without_raw_error(caplog):
    class Unavailable:
        def throttle(self, *args):
            raise OperationalError('SQL private', {'password':'private'}, RuntimeError('certificate private'))
    with TestClient(create_app(WebSettings(origin='http://testserver'), Unavailable())) as client:
        response = client.post('/api/login',json={'usuario':'cristiano','senha':'not-a-real-password'},headers={'Origin':'http://testserver'})
    assert response.status_code == 503
    assert response.json()['erro'] == 'BANCO_INDISPONIVEL'
    assert 'private' not in response.text + caplog.text
    assert 'DB_CERTIFICATE' in caplog.text
