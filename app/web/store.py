"""Persistência comercial separada. Transações curtas e um lock por organização."""
import hashlib
import json
import secrets
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import (MetaData, Table, Column, String, Integer, Float, Text, CheckConstraint,
                        ForeignKey, UniqueConstraint, create_engine, select, update, insert, delete, func, event)
from sqlalchemy.pool import NullPool
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert

metadata = MetaData()
organizations = Table('web_organizations', metadata,
    Column('id', String(80), primary_key=True), Column('initial_tokens', Integer, nullable=False),
    Column('consumed', Integer, nullable=False), Column('reserved', Integer, nullable=False),
    CheckConstraint('initial_tokens >= 0 AND consumed >= 0 AND reserved >= 0 AND consumed + reserved <= initial_tokens'))
users = Table('web_users', metadata,
    Column('id', String(40), primary_key=True), Column('username', String(120), unique=True, nullable=False),
    Column('password_hash', Text, nullable=False), Column('organization_id', String(80), ForeignKey(organizations.c.id), nullable=False),
    Column('active', Integer, nullable=False))
sessions = Table('web_sessions', metadata,
    Column('token_hash', String(64), primary_key=True), Column('user_id', String(40), ForeignKey(users.c.id), nullable=False),
    Column('csrf', String(80), nullable=False), Column('expires_at', Float, nullable=False))
companies = Table('web_companies', metadata,
    Column('organization_id', String(80), ForeignKey(organizations.c.id), primary_key=True),
    Column('cnpj', String(14), primary_key=True), Column('payload', Text, nullable=False), Column('source_hash', String(64), nullable=False))
consultations = Table('web_consultations', metadata,
    Column('id', String(40), primary_key=True), Column('organization_id', String(80), ForeignKey(organizations.c.id), nullable=False),
    Column('user_id', String(40), ForeignKey(users.c.id), nullable=False), Column('idempotency_key', String(80), nullable=False),
    Column('cnpj', String(14), nullable=False), Column('status', String(24), nullable=False),
    Column('result', Text), Column('created_at', Float, nullable=False), Column('finished_at', Float),
    UniqueConstraint('organization_id', 'idempotency_key'))
charges = Table('web_token_ledger', metadata,
    Column('consultation_id', String(40), ForeignKey(consultations.c.id), primary_key=True),
    Column('organization_id', String(80), ForeignKey(organizations.c.id), nullable=False),
    Column('amount', Integer, nullable=False), CheckConstraint('amount = 1'), Column('created_at', Float, nullable=False))
imports = Table('web_imports', metadata,
    Column('organization_id', String(80), ForeignKey(organizations.c.id), primary_key=True),
    Column('source_hash', String(64), primary_key=True), Column('record_count', Integer, nullable=False),
    Column('created_at', Float, nullable=False))
import_records = Table('web_import_records', metadata,
    Column('organization_id', String(80), ForeignKey(organizations.c.id), primary_key=True),
    Column('source_hash', String(64), primary_key=True), Column('cnpj', String(14), primary_key=True),
    Column('record', Text, nullable=False))
rates = Table('web_rate_limits', metadata,
    Column('key', String(160), primary_key=True), Column('started_at', Float, nullable=False), Column('count', Integer, nullable=False))

batches = Table('web_batches', metadata,
    Column('id', String(40), primary_key=True),
    Column('organization_id', String(80), ForeignKey(organizations.c.id), nullable=False),
    Column('user_id', String(40), ForeignKey(users.c.id), nullable=False),
    Column('idempotency_key', String(80), nullable=False), Column('input_hash', String(64), nullable=False),
    Column('input_json', Text, nullable=False), Column('created_at', Float, nullable=False),
    UniqueConstraint('organization_id', 'idempotency_key'))
batch_items = Table('web_batch_items', metadata,
    Column('batch_id', String(40), ForeignKey(batches.c.id), primary_key=True),
    Column('position', Integer, primary_key=True),
    Column('consultation_id', String(40), ForeignKey(consultations.c.id), unique=True, nullable=False),
    Column('error_code', String(60)), CheckConstraint('position >= 0 AND position < 15'))
external_control = Table('web_external_control', metadata,
    Column('id', String(30), primary_key=True), Column('policy', Text, nullable=False),
    Column('lease_until', Float, nullable=False))
external_requests = Table('web_external_requests', metadata,
    Column('id', String(80), primary_key=True), Column('provider', String(30), nullable=False),
    Column('started_at', Float, nullable=False), Column('outcome', String(40), nullable=False),
    Column('http_status', Integer), Column('operation', String(120), unique=True, nullable=False))
live_jobs = Table('web_live_jobs', metadata,
    Column('cnpj', String(14), primary_key=True), Column('id', String(40), unique=True, nullable=False),
    Column('status', String(30), nullable=False), Column('state', Text, nullable=False),
    Column('step', Integer, nullable=False), Column('lease_until', Float, nullable=False),
    Column('result', Text), Column('error', String(80)), Column('updated_at', Float, nullable=False))


class WebError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


# Regra de tokens aprovada em 10/11/2026: cada pesquisa real concluída custa
# exatamente 1 token; reaproveitar um resultado já salvo não custa nada.
REUSED_RESULT = 'Resultado já salvo de uma pesquisa anterior, reaproveitado sem nova busca. Nenhum token foi consumido.'


class Store:
    def __init__(self, url):
        if not url:
            raise WebError('CONFIGURACAO_PENDENTE', 503)
        if url.startswith('sqlite:///'):
            Path(url.removeprefix('sqlite:///')).resolve().parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, poolclass=NullPool,
            connect_args={'timeout': 30, 'check_same_thread': False} if url.startswith('sqlite:') else {'connect_timeout': 8})
        if self.engine.dialect.name == 'sqlite':
            @event.listens_for(self.engine, 'connect')
            def pragmas(dbapi, _):
                dbapi.execute('PRAGMA foreign_keys=ON')
                dbapi.execute('PRAGMA busy_timeout=30000')

    @contextmanager
    def transaction(self):
        with self.engine.connect() as conn:
            if self.engine.dialect.name == 'sqlite':
                conn.exec_driver_sql('BEGIN IMMEDIATE')
            else:
                conn.begin()
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def upsert(self, conn, table, values, keys, changes=None):
        stmt = (sqlite_insert if self.engine.dialect.name == 'sqlite' else pg_insert)(table).values(**values)
        stmt = stmt.on_conflict_do_update(index_elements=keys, set_=changes) if changes else stmt.on_conflict_do_nothing(index_elements=keys)
        # psycopg may report rowcount=-1. RETURNING is authoritative on both engines.
        return conn.execute(stmt.returning(table.c[keys[0]])).scalar_one_or_none()

    def initialize(self, organization='dazumdigital'):
        # DDL is an explicit administrative action; never reset an existing balance.
        from sqlalchemy import inspect
        if {'companies', 'requests', 'consolidated_companies'} & set(inspect(self.engine).get_table_names()):
            raise ValueError('Arquivo histórico não pode ser inicializado como banco web')
        metadata.create_all(self.engine)
        with self.transaction() as conn:
            self.upsert(conn, organizations, dict(id=organization, initial_tokens=2469, consumed=0, reserved=0), ['id'])

    def lock_org(self, conn, organization):
        row = conn.execute(select(organizations).where(organizations.c.id == organization).with_for_update()).mappings().first()
        if not row:
            raise WebError('CONFIGURACAO_PENDENTE', 503)
        return row

    def balance(self, organization, conn=None):
        if conn is None:
            with self.engine.connect() as c:
                return self.balance(organization, c)
        row = conn.execute(select(organizations).where(organizations.c.id == organization)).mappings().one()
        return dict(disponiveis=row['initial_tokens'] - row['consumed'] - row['reserved'], consumidos=row['consumed'],
                    reservados=row['reserved'], consultas_concluidas=row['consumed'])

    def throttle(self, key, limit, seconds=60, code='MUITAS_TENTATIVAS'):
        now = time.time()
        with self.transaction() as conn:
            self.upsert(conn, rates, dict(key=key, started_at=now, count=0), ['key'])
            row = conn.execute(select(rates).where(rates.c.key == key).with_for_update()).mappings().one()
            count, start = (0, now) if now - row['started_at'] >= seconds else (row['count'], row['started_at'])
            if count >= limit:
                raise WebError(code, 429)
            conn.execute(update(rates).where(rates.c.key == key).values(count=count+1, started_at=start))

    def authenticate(self, token):
        with self.engine.connect() as conn:
            row = conn.execute(select(users.c.id, users.c.organization_id, users.c.username, sessions.c.csrf)
                .join(sessions, sessions.c.user_id == users.c.id).where(sessions.c.token_hash == digest(token or ''),
                    sessions.c.expires_at > time.time(), users.c.active == 1)).mappings().first()
        if not row:
            raise WebError('NAO_AUTORIZADO', 401)
        return dict(row)

    def view(self, row, conn):
        return dict(id=row['id'], cnpj=row['cnpj'], status=row['status'], resultado=json.loads(row['result']) if row['result'] else None,
                    criado_em=row['created_at'], saldo=self.balance(row['organization_id'], conn),
                    erro=self.failure_code(conn, row) if row['status'] == 'FALHOU' else None,
                    token_consumido=conn.execute(select(charges.c.consultation_id).where(
                        charges.c.consultation_id == row['id'])).first() is not None,
                    revalidacao_pendente=row['status'] == 'CONCLUIDA' and self.revalidation_pending(conn, row['cnpj']))

    def revalidation_pending(self, conn, cnpj):
        """Revalidação de sites autorizada pelo administrador e ainda não concluída."""
        job = conn.execute(select(live_jobs.c.status, live_jobs.c.state).where(live_jobs.c.cnpj == cnpj)).first()
        if not job or job[0] not in ('READY', 'IN_FLIGHT'):
            return False
        try:
            return json.loads(job[1]).get('revalidation') is True
        except (TypeError, ValueError):
            return False

    def failure_code(self, conn, row):
        """Código operacional fixo da falha; nunca mensagens brutas de provedores."""
        code = conn.execute(select(batch_items.c.error_code).where(batch_items.c.consultation_id == row['id'])).scalar_one_or_none()
        if code:
            return code
        return conn.execute(select(live_jobs.c.error).where(live_jobs.c.cnpj == row['cnpj'],
            live_jobs.c.status.in_(['FAILED', 'UNCERTAIN']))).scalar_one_or_none()

    def consultation(self, organization, identifier):
        with self.engine.connect() as conn:
            row = conn.execute(select(consultations).where(consultations.c.organization_id == organization, consultations.c.id == identifier)).mappings().first()
            if not row:
                raise WebError('NAO_ENCONTRADO', 404)
            return self.view(row, conn)

    def complete(self, conn, row, payload, reserved=False, charge=True):
        """Conclui a consulta na mesma transação do desconto. Pesquisa real
        concluída: 1 token (charge=True). Reaproveitamento: nenhum. O ledger tem
        a consulta como chave primária e impede débito duplicado."""
        if charge:
            conn.execute(insert(charges).values(consultation_id=row['id'], organization_id=row['organization_id'], amount=1, created_at=time.time()))
            conn.execute(update(organizations).where(organizations.c.id == row['organization_id']).values(
                consumed=organizations.c.consumed+1, reserved=organizations.c.reserved-(1 if reserved else 0)))
        else:
            payload = dict(payload, ressalvas=list(payload.get('ressalvas', [])) + [REUSED_RESULT])
            if reserved:
                conn.execute(update(organizations).where(organizations.c.id == row['organization_id']).values(
                    reserved=organizations.c.reserved-1))
        packed = json.dumps(payload, ensure_ascii=False)
        conn.execute(update(consultations).where(consultations.c.id == row['id']).values(
            status='CONCLUIDA', result=packed, finished_at=time.time()))
        return packed

    def consult(self, user, cnpj, key, allow_external=False, external_authorized=False):
        with self.transaction() as conn:
            org = self.lock_org(conn, user['organization_id'])
            old = conn.execute(select(consultations).where(consultations.c.organization_id == org['id'], consultations.c.idempotency_key == key)).mappings().first()
            if old:
                if old['cnpj'] != cnpj or old['user_id'] != user['id']:
                    raise WebError('CHAVE_REUTILIZADA', 409)
                return self.view(old, conn)
            saved = self.saved_payload(conn, org['id'], cnpj)
            # Reaproveitar não custa token; só a pesquisa nova exige saldo.
            if not saved and org['initial_tokens'] - org['consumed'] - org['reserved'] <= 0:
                raise WebError('SALDO_INSUFICIENTE', 402)
            if not saved and not (allow_external and external_authorized):
                raise WebError('EMPRESA_NAO_PESQUISADA', 404)
            row = dict(id=str(uuid.uuid4()), organization_id=org['id'], user_id=user['id'], idempotency_key=key,
                       cnpj=cnpj, status='NA_FILA', result=None, created_at=time.time(), finished_at=None)
            conn.execute(insert(consultations).values(**row))
            if saved:
                payload = json.loads(saved)
                payload['origem_resultado'] = 'HISTORICO'
                row.update(status='CONCLUIDA', result=self.complete(conn, row, payload, charge=False))
            else:
                conn.execute(update(organizations).where(organizations.c.id == org['id']).values(reserved=organizations.c.reserved+1))
            return self.view(row, conn)

    def saved_payload(self, conn, organization, cnpj):
        return conn.execute(select(companies.c.payload).where(
            companies.c.organization_id == organization, companies.c.cnpj == cnpj)).scalar_one_or_none()

    def claim(self, organization):
        with self.transaction() as conn:
            self.lock_org(conn, organization)
            row = conn.execute(select(consultations).where(consultations.c.organization_id == organization,
                consultations.c.status == 'NA_FILA').order_by(consultations.c.created_at).limit(1)).mappings().first()
            if not row:
                return None
            conn.execute(update(consultations).where(consultations.c.id == row['id']).values(status='EM_PROCESSAMENTO'))
            return dict(row)

    def finish_job(self, identifier, payload=None):
        with self.transaction() as conn:
            initial = conn.execute(select(consultations).where(consultations.c.id == identifier)).mappings().one()
            self.lock_org(conn, initial['organization_id'])
            row = conn.execute(select(consultations).where(consultations.c.id == identifier)).mappings().one()
            if row['status'] not in {'EM_PROCESSAMENTO', 'NA_FILA'}:
                return
            if payload is None:
                conn.execute(update(organizations).where(organizations.c.id == row['organization_id']).values(reserved=organizations.c.reserved-1))
                conn.execute(update(consultations).where(consultations.c.id == identifier).values(status='FALHOU', finished_at=time.time()))
            else:
                if payload.get('empresa', {}).get('cnpj') != row['cnpj']:
                    raise ValueError('Identidade do resultado diferente da consulta')
                packed = json.dumps(payload, ensure_ascii=False)
                self.upsert(conn, companies, dict(organization_id=row['organization_id'], cnpj=row['cnpj'], payload=packed, source_hash=digest(packed)),
                            ['organization_id', 'cnpj'], dict(payload=packed, source_hash=digest(packed)))
                self.complete(conn, row, payload, reserved=True)
