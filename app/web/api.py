import hmac
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.services.importer import valid_cnpj
from app.web import auth
from app.web.config import WebSettings
from app.web.store import Store, WebError, consultations, digest
from app.web.diagnostics import log_database_error

STATIC = Path(__file__).parent / 'static'
MESSAGES = {
    'CNPJ_INVALIDO': 'O CNPJ informado é inválido. Confira os números e tente novamente.',
    'SALDO_INSUFICIENTE': 'Você não possui tokens suficientes para realizar uma nova consulta.',
    'EMPRESA_NAO_PESQUISADA': 'Este CNPJ ainda não possui uma pesquisa registrada em nossa base.',
    'NAO_AUTORIZADO': 'Entre com sua conta para continuar.', 'CREDENCIAIS_INVALIDAS': 'Usuário ou senha inválidos.',
    'MUITAS_TENTATIVAS': 'Muitas tentativas. Aguarde um minuto e tente novamente.',
    'CHAVE_REUTILIZADA': 'Esta operação já foi utilizada para outro CNPJ.',
    'ORIGEM_INVALIDA': 'Atualize a página e tente novamente.', 'CSRF_INVALIDO': 'Atualize a página e tente novamente.',
    'CONFIGURACAO_PENDENTE': 'A plataforma aguarda configuração pelo responsável.',
    'NAO_ENCONTRADO': 'Consulta não encontrada.', 'REQUISICAO_INVALIDA': 'Confira os dados informados.',
    'ERRO_TECNICO': 'Não foi possível concluir a consulta. Tente novamente.',
    'BANCO_INDISPONIVEL': 'A conexão com o banco está temporariamente indisponível. Tente novamente em instantes.',
}


class LoginBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    usuario: str = Field(min_length=3, max_length=120)
    senha: str = Field(min_length=1, max_length=256)


class QueryBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cnpj: str = Field(min_length=1, max_length=24)
    autorizar_pesquisa_externa: bool = False


def create_app(settings=None, store=None):
    config = settings or WebSettings.from_env()
    app = FastAPI(title='DazumDigital | Consulta de CNPJ', docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(config.origin).hostname])
    app.state.store = store

    def database():
        if app.state.store is None:
            app.state.store = Store(config.database_url)
        return app.state.store

    def identity(request, mutation=False):
        token = request.cookies.get('dazum_session', '')
        if not token or len(token) > 128:
            raise WebError('NAO_AUTORIZADO', 401)
        user = database().authenticate(token)
        if mutation:
            origin(request)
            if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), user['csrf']):
                raise WebError('CSRF_INVALIDO', 403)
        return user

    def origin(request):
        if request.headers.get('origin') != config.origin:
            raise WebError('ORIGEM_INVALIDA', 403)

    @app.exception_handler(WebError)
    async def known_error(request, exc):
        return JSONResponse(dict(erro=exc.code, mensagem=MESSAGES[exc.code]), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request, exc):
        return JSONResponse(dict(erro='REQUISICAO_INVALIDA', mensagem=MESSAGES['REQUISICAO_INVALIDA']), status_code=422)

    @app.exception_handler(OperationalError)
    async def database_failure(request, exc):
        log_database_error(exc)
        return JSONResponse(dict(erro='BANCO_INDISPONIVEL', mensagem=MESSAGES['BANCO_INDISPONIVEL']), status_code=503)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        logging.getLogger(__name__).error('Falha web: %s', type(exc).__name__)
        return JSONResponse(dict(erro='ERRO_TECNICO', mensagem=MESSAGES['ERRO_TECNICO']), status_code=500)

    @app.middleware('http')
    async def security_headers(request, call_next):
        if request.method in {'POST', 'PUT', 'PATCH'}:
            try:
                size = int(request.headers.get('content-length', '0'))
            except ValueError:
                size = 99999
            if size > 4096:
                return JSONResponse({'mensagem': MESSAGES['REQUISICAO_INVALIDA']}, status_code=413)
            # Also bound chunked bodies; JSON endpoints are intentionally small.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 4096:
                    return JSONResponse({'mensagem': MESSAGES['REQUISICAO_INVALIDA']}, status_code=413)
            request._body = bytes(body)
        try:
            response = await call_next(request)
        except Exception as exc:
            # Prevent the ASGI server from logging SQL parameter dumps or raw
            # exception messages after a handled 500 response.
            logging.getLogger(__name__).error('Falha web: %s', type(exc).__name__)
            response = JSONResponse(dict(erro='ERRO_TECNICO', mensagem=MESSAGES['ERRO_TECNICO']), status_code=500)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if config.secure_cookie:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    @app.get('/api/health')
    def health():
        try:
            with database().engine.connect() as conn:
                conn.execute(select(consultations.c.id).limit(1))
            return dict(status='ok')
        except Exception as exc:
            log_database_error(exc)
            return JSONResponse({'status': 'indisponivel'}, status_code=503)

    @app.post('/api/login')
    def login(body: LoginBody, request: Request):
        origin(request)
        db = database()
        address = request.client.host if request.client else 'unknown'
        db.throttle('login-ip:' + digest(address), 10)
        db.throttle('login-user:' + digest(body.usuario.strip().lower()), 5)
        old = request.cookies.get('dazum_session')
        token, csrf = auth.login(db, body.usuario, body.senha, config.session_hours)
        if old:
            auth.revoke(db, old)
        response = JSONResponse(dict(csrf=csrf))
        response.set_cookie('dazum_session', token, httponly=True, secure=config.secure_cookie, samesite='strict', max_age=config.session_hours*3600, path='/')
        return response

    @app.get('/api/sessao')
    def session(request: Request):
        try:
            user = identity(request)
        except WebError as exc:
            if exc.code == 'NAO_AUTORIZADO':
                return dict(autenticado=False)
            raise
        return dict(autenticado=True, usuario=user['username'], csrf=user['csrf'], pesquisa_externa_disponivel=config.external_enabled)

    @app.post('/api/logout')
    def logout(request: Request):
        identity(request, True)
        auth.revoke(database(), request.cookies['dazum_session'])
        response = JSONResponse({'status': 'ok'})
        response.delete_cookie('dazum_session', path='/')
        return response

    @app.get('/api/saldo')
    def balance(request: Request):
        user = identity(request)
        return database().balance(user['organization_id'])

    @app.post('/api/consultas')
    def consult(body: QueryBody, request: Request):
        user = identity(request, True)
        if not re.fullmatch(r'[0-9./\-\s]+', body.cnpj):
            raise WebError('CNPJ_INVALIDO', 422)
        cnpj = re.sub(r'[^0-9]', '', body.cnpj)
        if not valid_cnpj(cnpj):
            raise WebError('CNPJ_INVALIDO', 422)
        key = request.headers.get('idempotency-key', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', key):
            raise WebError('REQUISICAO_INVALIDA', 422)
        database().throttle('query:' + user['organization_id'], config.query_per_minute)
        return database().consult(user, cnpj, key, body.autorizar_pesquisa_externa, config.external_enabled)

    @app.get('/api/consultas')
    def history(request: Request):
        user = identity(request)
        with database().engine.connect() as conn:
            rows = conn.execute(select(consultations).where(consultations.c.organization_id == user['organization_id'])
                .order_by(consultations.c.created_at.desc()).limit(10)).mappings().all()
            return [dict(id=r['id'], cnpj=r['cnpj'], status=r['status'], criado_em=r['created_at']) for r in rows]

    @app.get('/api/consultas/{identifier}')
    def query(identifier: str, request: Request):
        return database().consultation(identity(request)['organization_id'], identifier)

    @app.get('/')
    def home():
        return FileResponse(STATIC / 'index.html')

    @app.get('/favicon.ico')
    def favicon():
        return FileResponse(STATIC / 'favicon.ico', media_type='image/x-icon')

    app.mount('/assets', StaticFiles(directory=STATIC), name='assets')
    return app


app = create_app()
