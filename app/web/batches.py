"""Lotes duráveis de dados armazenados. Cada chamada processa só uma empresa.

Nenhum provedor externo é chamado aqui. As reservas e os resultados usam o
mesmo lock de organização e o mesmo ledger da consulta individual.
"""
import json
import time
import uuid
from sqlalchemy import select, insert, update
from app.web.batch_input import validated_input
from app.web.store import batches, batch_items, consultations, companies, organizations, WebError, digest


def owned(conn, user, identifier):
    row = conn.execute(select(batches).where(batches.c.id == identifier,
        batches.c.organization_id == user['organization_id'], batches.c.user_id == user['id'])).mappings().first()
    if not row:
        raise WebError('NAO_ENCONTRADO', 404)
    return row


def view(store, conn, user, identifier):
    row = owned(conn, user, identifier)
    records = conn.execute(select(consultations, batch_items.c.position, batch_items.c.error_code)
        .join(batch_items, batch_items.c.consultation_id == consultations.c.id)
        .where(batch_items.c.batch_id == identifier).order_by(batch_items.c.position)).mappings().all()
    items = [dict(id=r['id'], cnpj=r['cnpj'], status=r['status'], erro=r['error_code'],
                  criado_em=r['created_at'], resultado=json.loads(r['result']) if r['result'] else None) for r in records]
    done = sum(i['status'] == 'CONCLUIDA' for i in items)
    errors = sum(i['status'] == 'FALHOU' for i in items)
    running = sum(i['status'] == 'EM_PROCESSAMENTO' for i in items)
    pending = len(items) - done - errors - running
    status = ('CONCLUIDO_COM_ERROS' if errors else 'CONCLUIDO') if not pending and not running else ('EM_ANDAMENTO' if done or errors or running else 'PENDENTE')
    return dict(id=identifier, status=status, criado_em=row['created_at'], entrada=json.loads(row['input_json']),
        progresso=dict(total=len(items), concluidos=done, em_andamento=running, pendentes=pending,
                       erros=errors, tokens_consumidos=done), itens=items, saldo=store.balance(user['organization_id'], conn),
        modo='DADOS_ARMAZENADOS', pesquisa_externa=False)


def get_batch(store, user, identifier):
    with store.engine.connect() as conn:
        return view(store, conn, user, identifier)


def create_batch(store, user, value, key):
    parsed = validated_input(value)
    packed = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
    fingerprint = digest(packed)
    with store.transaction() as conn:
        org = store.lock_org(conn, user['organization_id'])
        old = conn.execute(select(batches).where(batches.c.organization_id == org['id'], batches.c.idempotency_key == key)).mappings().first()
        if old:
            if old['user_id'] != user['id'] or old['input_hash'] != fingerprint:
                raise WebError('CHAVE_REUTILIZADA', 409)
            return view(store, conn, user, old['id'])
        count = parsed['validos']
        if org['initial_tokens'] - org['consumed'] - org['reserved'] < count:
            raise WebError('SALDO_INSUFICIENTE', 402)
        identifier = str(uuid.uuid4())
        now = time.time()
        conn.execute(insert(batches).values(id=identifier, organization_id=org['id'], user_id=user['id'],
            idempotency_key=key, input_hash=fingerprint, input_json=packed, created_at=now))
        for position, cnpj in enumerate(parsed['cnpjs']):
            query_id = str(uuid.uuid4())
            conn.execute(insert(consultations).values(id=query_id, organization_id=org['id'], user_id=user['id'],
                idempotency_key='batch_' + query_id, cnpj=cnpj, status='PENDENTE_LOTE', created_at=now))
            conn.execute(insert(batch_items).values(batch_id=identifier, position=position, consultation_id=query_id))
        conn.execute(update(organizations).where(organizations.c.id == org['id']).values(reserved=organizations.c.reserved + count))
        return view(store, conn, user, identifier)


def fail_item(conn, row, error):
    conn.execute(update(organizations).where(organizations.c.id == row['organization_id']).values(reserved=organizations.c.reserved - 1))
    conn.execute(update(consultations).where(consultations.c.id == row['id']).values(status='FALHOU', finished_at=time.time()))
    conn.execute(update(batch_items).where(batch_items.c.consultation_id == row['id']).values(error_code=error))


def step_batch(store, user, identifier, item_id, external=False, key=''):
    # The item ID identifies the step. A retry can never advance to a different item.
    with store.transaction() as conn:
        store.lock_org(conn, user['organization_id'])
        owned(conn, user, identifier)
        row = conn.execute(select(consultations).join(batch_items, batch_items.c.consultation_id == consultations.c.id)
            .where(batch_items.c.batch_id == identifier, consultations.c.id == item_id)).mappings().first()
        if not row:
            raise WebError('NAO_ENCONTRADO', 404)
        if row['status'] not in {'PENDENTE_LOTE','EM_PROCESSAMENTO'}:
            return view(store, conn, user, identifier)
        saved = store.saved_payload(conn, user['organization_id'], row['cnpj'])
        if not saved and external:
            pass  # Networking runs after the transaction and org lock are released.
        elif not saved:
            fail_item(conn, row, 'EMPRESA_NAO_PESQUISADA')
        else:
            try:
                payload = json.loads(saved)
                payload['origem_resultado'] = 'HISTORICO'
                if payload.get('empresa', {}).get('cnpj') != row['cnpj']:
                    raise ValueError('Identidade divergente')
            except (ValueError, TypeError, AttributeError):
                fail_item(conn, row, 'DADOS_ARMAZENADOS_INVALIDOS')
            else:
                store.complete(conn, row, payload, reserved=True)
        if saved or not external:
            return view(store, conn, user, identifier)
    from app.web.live import process_consultation
    state=process_consultation(store,user,item_id,key)
    data=get_batch(store,user,identifier)
    data.update(retry_after=state.get('retry_after',0),etapa_pesquisa=state.get('etapa_pesquisa'))
    return data


def cancel_batch(store, user, identifier):
    with store.transaction() as conn:
        store.lock_org(conn, user['organization_id'])
        owned(conn, user, identifier)
        rows = conn.execute(select(consultations).join(batch_items, batch_items.c.consultation_id == consultations.c.id)
            .where(batch_items.c.batch_id == identifier, consultations.c.status.in_(['PENDENTE_LOTE','EM_PROCESSAMENTO']))).mappings().all()
        for row in rows:
            fail_item(conn, row, 'CANCELADA')
        return view(store, conn, user, identifier)


def list_batches(store, user):
    with store.engine.connect() as conn:
        rows = conn.execute(select(batches.c.id).where(batches.c.organization_id == user['organization_id'],
            batches.c.user_id == user['id']).order_by(batches.c.created_at.desc()).limit(20)).scalars().all()
        output = []
        for identifier in rows:
            data = view(store, conn, user, identifier)
            output.append({k:data[k] for k in ['id', 'status', 'criado_em', 'progresso']})
        return output
