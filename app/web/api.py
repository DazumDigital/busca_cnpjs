import hmac
import ipaddress
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.services.importer import valid_cnpj
from app.web import auth
from app.web.config import WebSettings
from app.web.store import Store, WebError, consultations, digest, batch_items, companies
from app.web.diagnostics import log_database_error
from app.web import batches as batch_service
from app.web.exports import export
from app.web import live

STATIC = Path(__file__).parent / 'static'
MESSAGES = {
    'CNPJ_INVALIDO': 'O CNPJ informado é inválido. Confira os números e tente novamente.',
    'SALDO_INSUFICIENTE': 'Você não possui tokens suficientes para realizar uma nova consulta.',
    'EMPRESA_NAO_PESQUISADA': 'CNPJ sem resultado armazenado. A pesquisa externa está desativada neste ambiente.',
    'NAO_AUTORIZADO': 'Entre com sua conta para continuar.', 'CREDENCIAIS_INVALIDAS': 'Usuário ou senha inválidos.',
    'MUITAS_TENTATIVAS': 'Muitas tentativas. Aguarde um minuto e tente novamente.',
    'CHAVE_REUTILIZADA': 'Esta operação já foi utilizada para outro CNPJ.',
    'ORIGEM_INVALIDA': 'Atualize a página e tente novamente.', 'CSRF_INVALIDO': 'Atualize a página e tente novamente.',
    'CONFIGURACAO_PENDENTE': 'A plataforma aguarda configuração pelo responsável.',
    'NAO_ENCONTRADO': 'Consulta não encontrada.', 'REQUISICAO_INVALIDA': 'Confira os dados informados.',
    'ERRO_TECNICO': 'Não foi possível concluir a consulta. Tente novamente.',
    'BANCO_INDISPONIVEL': 'A conexão com o banco está temporariamente indisponível. Tente novamente em instantes.',
    'LIMITE_LOTE': 'Informe no máximo 15 CNPJs válidos e distintos por lote.',
    'INTEGRACAO_NAO_VERIFICADA': 'A pesquisa externa aguarda configuração ou renovação da confirmação gratuita pelo responsável.',
    'PESQUISA_SUSPENSA': 'A pesquisa externa está suspensa pelo responsável. Nenhuma chamada foi enviada e nenhum token foi consumido.',
    'PESQUISA_NAO_LIBERADA': 'Sua conta não está liberada para pesquisas externas deste CNPJ. Fale com o responsável. Nenhum token foi consumido.',
    'LIMITE_CONSULTAS': 'Limite de consultas por minuto atingido. Aguarde um minuto e tente novamente; nada foi cobrado.',
    'COTA_EXTERNA_ESGOTADA': 'O limite de pesquisas externas aprovado foi atingido. Nenhuma nova chamada foi enviada e nenhum token foi consumido.',
    'RESERVA_SERPER': 'Pesquisas novas pausadas para preservar a reserva mínima de 100 créditos da Serper. Nenhuma chamada foi enviada e nenhum token foi consumido. Fale com o responsável.',
}


class LoginBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    usuario: str = Field(min_length=3, max_length=120)
    senha: str = Field(min_length=1, max_length=256)


class QueryBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cnpj: str = Field(min_length=1, max_length=24)
    autorizar_pesquisa_externa: bool = False


class BatchBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cnpjs: str = Field(min_length=1, max_length=3500)


def company_name(result):
    """Nome exibido no histórico (fantasia ou razão social), só a partir do resultado salvo."""
    try:
        company = (json.loads(result) or {}).get('empresa') or {}
        return str(company.get('nome_fantasia') or company.get('razao_social') or '')[:120]
    except (TypeError, ValueError, AttributeError):
        return ''


def client_address(request):
    # Na Vercel, x-forwarded-for traz o IP público do cliente e é sobrescrito pela
    # plataforma para impedir falsificação (docs: Request headers). Fora dela,
    # vale o endereço da conexão.
    if os.getenv('VERCEL'):
        try:
            return str(ipaddress.ip_address(request.headers.get('x-forwarded-for', '').split(',')[0].strip()))
        except ValueError:
            pass
    return request.client.host if request.client else 'unknown'


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
        db.throttle('login-ip:' + digest(client_address(request)), 10)
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
        external=config.external_enabled
        if external:
            try:live.ensure_ready(database(),config.serper_key,user['organization_id'])
            except WebError:external=False
        return dict(autenticado=True, usuario=user['username'], csrf=user['csrf'], pesquisa_externa_disponivel=external)

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
        database().throttle('query:' + user['organization_id'], config.query_per_minute, code='LIMITE_CONSULTAS')
        if config.external_enabled:
            with database().engine.connect() as conn:
                saved=database().saved_payload(conn,user['organization_id'],cnpj)
            if not saved:live.ensure_ready(database(),config.serper_key,user['organization_id'],(cnpj,))
        return database().consult(user, cnpj, key, body.autorizar_pesquisa_externa or config.external_enabled, config.external_enabled)

    @app.get('/api/consultas')
    def history(request: Request):
        user = identity(request)
        with database().engine.connect() as conn:
            rows = conn.execute(select(consultations).where(consultations.c.organization_id == user['organization_id'], consultations.c.user_id == user['id'],
                ~consultations.c.id.in_(select(batch_items.c.consultation_id)))
                .order_by(consultations.c.created_at.desc()).limit(20)).mappings().all()
            return [dict(id=r['id'], cnpj=r['cnpj'], status=r['status'], criado_em=r['created_at'], empresa=company_name(r['result'])) for r in rows]

    @app.get('/api/consultas/{identifier}')
    def query(identifier: str, request: Request):
        user = identity(request)
        return individual(user, identifier)

    def individual(user, identifier):
        with database().engine.connect() as conn:
            owner = conn.execute(select(consultations.c.user_id).where(consultations.c.id == identifier,
                consultations.c.organization_id == user['organization_id'])).scalar_one_or_none()
        if owner != user['id']:
            raise WebError('NAO_ENCONTRADO', 404)
        return database().consultation(user['organization_id'], identifier)

    def download(items, formato, name):
        if formato not in {'csv', 'xlsx'}:
            raise WebError('REQUISICAO_INVALIDA', 422)
        content, media, extension = export(items, formato)
        return Response(content, media_type=media, headers={'Content-Disposition': f'attachment; filename="{name}.{extension}"'})

    @app.get('/api/consultas/{identifier}/exportar')
    def export_individual(identifier: str, request: Request, formato: str = 'csv'):
        data = individual(identity(request), identifier)
        return download([data], formato, 'consulta-' + data['cnpj'])

    @app.post('/api/consultas/{identifier}/processar')
    def process_individual(identifier: str, request: Request):
        user=identity(request,True)
        if not config.external_enabled:
            # Nunca deixa uma consulta presa na fila com token reservado.
            return live.settle_without_external(database(),user,identifier)
        return live.process_consultation(database(),user,identifier,config.serper_key)

    @app.post('/api/lotes')
    def create_batch(body: BatchBody, request: Request):
        user = identity(request, True)
        key = request.headers.get('idempotency-key', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', key):
            raise WebError('REQUISICAO_INVALIDA', 422)
        database().throttle('batch:' + user['organization_id'], config.query_per_minute, code='LIMITE_CONSULTAS')
        if config.external_enabled:
            from app.web.batch_input import validated_input
            parsed=validated_input(body.cnpjs)
            with database().engine.connect() as conn:
                missing=[cnpj for cnpj in parsed['cnpjs'] if not database().saved_payload(conn,user['organization_id'],cnpj)]
            if missing:live.ensure_ready(database(),config.serper_key,user['organization_id'],missing)
        return batch_service.create_batch(database(), user, body.cnpjs, key)

    @app.get('/api/lotes')
    def history_batches(request: Request):
        user = identity(request)
        return batch_service.list_batches(database(), user)

    @app.get('/api/lotes/{identifier}')
    def batch(identifier: str, request: Request):
        user = identity(request)
        return batch_service.get_batch(database(), user, identifier)

    @app.post('/api/lotes/{identifier}/itens/{item_id}/processar')
    def step_batch(identifier: str, item_id: str, request: Request):
        user = identity(request, True)
        return batch_service.step_batch(database(), user, identifier, item_id, config.external_enabled, config.serper_key)

    @app.post('/api/lotes/{identifier}/cancelar')
    def cancel_batch(identifier: str, request: Request):
        user = identity(request, True)
        return batch_service.cancel_batch(database(), user, identifier)

    @app.get('/api/lotes/{identifier}/exportar')
    def export_batch(identifier: str, request: Request, formato: str = 'csv'):
        user = identity(request)
        data = batch_service.get_batch(database(), user, identifier)
        return download(data['itens'], formato, 'lote-' + identifier)

    @app.get('/')
    def home():
        return FileResponse(STATIC / 'index.html')

    @app.get('/robo')
    def robot_info():
        # Página pública referenciada pelo User-Agent do robô de validação.
        return FileResponse(STATIC / 'robo.html')

    @app.get('/favicon.ico')
    def favicon():
        return FileResponse(STATIC / 'favicon.ico', media_type='image/x-icon')

    app.mount('/assets', StaticFiles(directory=STATIC), name='assets')
    return app


app = create_app()
