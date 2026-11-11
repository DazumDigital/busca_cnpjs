"""Regras do proprietário (11/11/2026).

1. O saldo da Serper nunca fica abaixo de 100 créditos: a busca só é reservada
   se, depois dela, ainda restarem 100 créditos do saldo confirmado.
2. Cada CNPJ novo concluído custa exatamente 1 token, também em lote (cobrança
   individual); resultado salvo é gratuito; bloqueio, falha e revalidação não cobram.
"""
import json
import time
import uuid

import pytest
from sqlalchemy import func, select

from test_web_platform import web, source, DAIJU
from test_web_batches import user
from test_web_live import live_setup, simulated, runner, ORG, KEY
from conftest import generated_cnpj
from app.web import batches as service
from app.web import live
from app.web.api import MESSAGES
from app.web.store import charges, external_control, external_requests

A, B, C = (generated_cnpj(880000 + i) for i in range(3))


def confirm(store, remaining):
    with store.engine.connect() as db:
        policy = json.loads(db.execute(select(external_control.c.policy)).scalar_one())
    policy.pop('key_sha256', None)
    policy['serper']['confirmed_free_remaining'] = remaining
    live.configure(store, policy, key=KEY)


def calls(store):
    with store.engine.connect() as db:
        return dict(db.execute(select(external_requests.c.provider, func.count()).group_by(external_requests.c.provider)).all())


def finish(store, identity, query):
    for _ in range(10):
        data = live.process_consultation(store, identity, query['id'], KEY, runner)
        if data['status'] != 'NA_FILA':
            return data
    raise AssertionError('consulta não terminou')


def test_reserve_allows_only_searches_that_keep_100_credits(live_setup):
    store = live_setup[1]
    identity = user(live_setup)
    confirm(store, 101)  # 101 confirmados: 1 busca deixa exatamente 100
    first = finish(store, identity, store.consult(identity, A, str(uuid.uuid4()), True, True))
    assert first['status'] == 'CONCLUIDA' and first['saldo']['consumidos'] == 1
    second = finish(store, identity, store.consult(identity, B, str(uuid.uuid4()), True, True))
    assert second['status'] == 'FALHOU' and second['erro'] == 'RESERVA_SERPER'
    assert second['saldo']['consumidos'] == 1 and second['saldo']['reservados'] == 0
    # Nem o cadastro gratuito é chamado quando a busca paga não pode acontecer.
    assert calls(store) == {'minhareceita': 1, 'serper': 1}


@pytest.mark.parametrize('remaining', [100, 50, 1])
def test_balance_at_or_below_reserve_blocks_every_new_search_without_cost(live_setup, remaining):
    store = live_setup[1]
    identity = user(live_setup)
    confirm(store, remaining)
    result = finish(store, identity, store.consult(identity, A, str(uuid.uuid4()), True, True))
    assert result['status'] == 'FALHOU' and result['erro'] == 'RESERVA_SERPER'
    assert result['saldo']['consumidos'] == 0 and result['saldo']['reservados'] == 0
    assert calls(store) == {}


def test_reserve_message_is_clear_and_promises_no_charge():
    assert '100 créditos' in MESSAGES['RESERVA_SERPER'] and 'nenhum token' in MESSAGES['RESERVA_SERPER']


def test_batch_charges_each_concluded_cnpj_once_and_reuses_saved_for_free(live_setup, monkeypatch):
    store = live_setup[1]
    identity = user(live_setup)
    original = live.process_consultation
    monkeypatch.setattr(live, 'process_consultation', lambda s, u, i, k: original(s, u, i, k, runner))
    batch = service.create_batch(store, identity, '\n'.join([A, B, DAIJU, A]), str(uuid.uuid4()))
    assert batch['progresso']['total'] == 3  # duplicado descartado
    data = batch
    for _ in range(20):
        pending = [i for i in data['itens'] if i['status'] in ('PENDENTE_LOTE', 'EM_PROCESSAMENTO')]
        if not pending:
            break
        for item in pending:
            data = service.step_batch(store, identity, batch['id'], item['id'], True, KEY)
            # Repetir o mesmo passo (clique duplo, nova tentativa) nunca cobra de novo.
            data = service.step_batch(store, identity, batch['id'], item['id'], True, KEY)
    by_cnpj = {i['cnpj']: i for i in data['itens']}
    assert by_cnpj[A]['token_consumido'] is True and by_cnpj[B]['token_consumido'] is True
    assert by_cnpj[DAIJU]['token_consumido'] is False and by_cnpj[DAIJU]['status'] == 'CONCLUIDA'
    assert data['progresso']['tokens_consumidos'] == 2
    assert data['saldo']['consumidos'] == 2 and data['saldo']['reservados'] == 0
    with store.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(charges)).scalar() == 2
    assert calls(store) == {'minhareceita': 2, 'serper': 2}
