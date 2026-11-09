"""Executor HTTP retomável, com uma única chamada externa por etapa.

Estado, reservas e leases ficam no banco persistente. Etapas interrompidas
após reserva nunca são repetidas automaticamente, pois o provedor pode tê-las
recebido. Não há threads de trabalho duráveis ou fila em memória.
"""
import asyncio
import json
import math
import time
import uuid
from sqlalchemy import select, insert, update
from app.config import policy_reason
from app.models import ProviderError
from app.web.store import (external_control, external_requests, live_jobs, consultations,
    companies, organizations, batch_items, WebError, digest)
from app.web.live_sources import execute_action, action_for, payload_for


def checked_policy(control, key):
    if not control or not key:
        raise WebError('INTEGRACAO_NAO_VERIFICADA',503)
    policy=json.loads(control['policy'])
    if policy_reason(policy['serper'],search=True) or policy.get('blocked'):
        raise WebError('INTEGRACAO_NAO_VERIFICADA',503)
    return policy


def ensure_ready(store,key,organization=None):
    with store.engine.connect() as conn:
        row=conn.execute(select(external_control).where(external_control.c.id=='global')).mappings().first()
        policy=checked_policy(row,key)
        if policy.get('allowed_organizations') and organization not in policy['allowed_organizations']:
            raise WebError('INTEGRACAO_NAO_VERIFICADA',503)
        return policy


def configure(store,policy,legacy=None):
    """Ação administrativa explícita. Não zera reservas ou consumo existentes."""
    if policy_reason(policy.get('serper',{}),search=True):
        raise ValueError('Confirmação gratuita inválida ou expirada')
    for name in ['minhareceita','websites']:
        p=policy[name]
        if any(type(p.get(k)) is not int or p[k]<=0 for k in ['max_requests','per_minute','per_hour','per_day']):
            raise ValueError('Limites de provedor inválidos')
    seed=[]
    if legacy:
        import sqlite3
        from pathlib import Path
        with sqlite3.connect('file:'+Path(legacy).resolve().as_posix()+'?mode=ro',uri=True) as db:
            seed=list(db.execute('SELECT id,provider,started_at,outcome,http_status FROM requests'))
    with store.transaction() as conn:
        for identifier,provider,started,outcome,status in seed:
            name=f'poc_sqlite:{provider}:{identifier}'
            store.upsert(conn,external_requests,dict(id=name,provider=provider,started_at=started,outcome=outcome,http_status=status,operation=name),['id'])
        previous=conn.execute(select(external_control).where(external_control.c.id=='global').with_for_update()).mappings().first()
        if previous and previous['lease_until']>time.time():raise ValueError('Há uma etapa externa em execução')
        store.upsert(conn,external_control,dict(id='global',policy=json.dumps(policy),lease_until=0),['id'],dict(policy=json.dumps(policy)))


def quota_delay(conn, control, policy, provider, now):
    p=policy[provider]
    rows=conn.execute(select(external_requests.c.started_at).where(external_requests.c.provider==provider)
        .order_by(external_requests.c.started_at)).scalars().all()
    if len(rows)>=p['max_requests']:raise WebError('COTA_EXTERNA_ESGOTADA',503)
    if provider=='serper':
        from datetime import datetime
        since=datetime.fromisoformat(p['balance_verified_at']).timestamp()
        if sum(t>=since for t in rows)>=p['confirmed_free_remaining']:raise WebError('COTA_EXTERNA_ESGOTADA',503)
    wait=max(0,control['lease_until']-now)
    wait=max(wait,policy.get('not_before',{}).get(provider,0)-now)
    for limit,seconds in [('per_minute',60),('per_hour',3600),('per_day',86400)]:
        recent=[t for t in rows if t>now-seconds]
        if len(recent)>=p[limit]:wait=max(wait,recent[-p[limit]]+seconds-now)
    if rows:wait=max(wait,rows[-1]+p.get('min_interval',0)-now)
    return max(0,math.ceil(wait))


def advance_job(store,cnpj,key,clock=time.time,executor=execute_action):
    now=clock()
    with store.transaction() as conn:
        control=conn.execute(select(external_control).where(external_control.c.id=='global').with_for_update()).mappings().first()
        policy=checked_policy(control,key)
        job=conn.execute(select(live_jobs).where(live_jobs.c.cnpj==cnpj).with_for_update()).mappings().first()
        if not job:
            identifier=str(uuid.uuid4())
            state=dict(phase='CADASTRO',created_at=now)
            conn.execute(insert(live_jobs).values(cnpj=cnpj,id=identifier,status='READY',state=json.dumps(state),
                step=0,lease_until=0,updated_at=now))
            job=dict(cnpj=cnpj,id=identifier,status='READY',state=json.dumps(state),step=0,lease_until=0,result=None,error=None)
        state=json.loads(job['state'])
        if job['status'] in {'DONE','FAILED','UNCERTAIN'}:
            return dict(status=job['status'],result=json.loads(job['result']) if job['result'] else None,error=job['error'])
        if job['status']=='IN_FLIGHT':
            if job['lease_until']>now:return dict(status='WAIT',retry_after=max(1,math.ceil(job['lease_until']-now)),phase=state['phase'])
            conn.execute(update(live_jobs).where(live_jobs.c.cnpj==cnpj).values(status='UNCERTAIN',error='ETAPA_INTERROMPIDA',updated_at=now))
            return dict(status='UNCERTAIN',error='ETAPA_INTERROMPIDA')
        provider=action_for(state)
        wait=quota_delay(conn,control,policy,provider,now)
        if provider=='websites' and state.get('last_web_at'):
            wait=max(wait,math.ceil(state['last_web_at']+state.get('crawl_delay',10)-now))
        if wait>0:return dict(status='WAIT',retry_after=wait,phase=state['phase'])
        operation=job['id']+':'+str(job['step'])
        lease_until = now + 40
        conn.execute(insert(external_requests).values(id=str(uuid.uuid4()),provider=provider,started_at=now,outcome='RESERVADA',operation=operation))
        conn.execute(update(external_control).where(external_control.c.id=='global').values(lease_until=lease_until))
        conn.execute(update(live_jobs).where(live_jobs.c.cnpj==cnpj).values(status='IN_FLIGHT',lease_until=lease_until,updated_at=now))
    error=None; result=None;cooldown=0
    try:
        # One fixed API request or one protected website fetch, with an 8s
        # timeout per transport. The budget stays below maxDuration=30.
        state=asyncio.run(executor(cnpj,state,key))
        if provider=='websites':state['last_web_at']=clock()
        done=state['phase']=='DONE'
        if done:result=payload_for(state)
    except Exception as exc:
        # Raw exceptions can carry URLs/credentials. Only our fixed codes leave here.
        error=exc.code if isinstance(exc,ProviderError) else 'FALHA_TECNICA_PESQUISA'
        if isinstance(exc,ProviderError):cooldown=min(86400,max(0,exc.details.get('retry_after',0)))
        done=False
        if state.get('company'):
            result=payload_for(state,partial=True)
    with store.transaction() as conn:
        control = conn.execute(select(external_control).where(external_control.c.id=='global').with_for_update()).mappings().one()
        current_job = conn.execute(select(live_jobs).where(live_jobs.c.cnpj==cnpj)).mappings().one()
        if (control['lease_until'] != lease_until or current_job['status'] != 'IN_FLIGHT'
                or current_job['step'] != job['step']):
            if current_job['status'] == 'IN_FLIGHT' and current_job['step'] == job['step']:
                conn.execute(update(live_jobs).where(live_jobs.c.cnpj==cnpj).values(
                    status='UNCERTAIN', error='ETAPA_INTERROMPIDA', updated_at=clock()))
            return dict(status='UNCERTAIN', error='ETAPA_INTERROMPIDA')
        if error in {'COTA_ESGOTADA','ACESSO_NEGADO'}:
            policy['blocked']=True
        if provider=='websites':cooldown=max(cooldown,state.get('crawl_delay',10))
        if cooldown:
            deadlines=dict(policy.get('not_before',{}));deadlines[provider]=clock()+cooldown;policy['not_before']=deadlines
        conn.execute(update(external_requests).where(external_requests.c.operation==operation).values(outcome='FALHA' if error else 'SUCESSO'))
        conn.execute(update(external_control).where(external_control.c.id=='global').values(lease_until=0,policy=json.dumps(policy)))
        conn.execute(update(live_jobs).where(live_jobs.c.cnpj==cnpj).values(state=json.dumps(state,ensure_ascii=False),
            status='FAILED' if error else 'DONE' if done else 'READY',step=job['step']+1,lease_until=0,
            result=json.dumps(result,ensure_ascii=False) if result else None,error=error,updated_at=clock()))
    return dict(status='FAILED' if error else 'DONE' if done else 'READY',result=result,error=error,
        phase=state['phase'],retry_after=0)


def process_consultation(store,user,identifier,key,runner=advance_job):
    with store.engine.connect() as conn:
        row=conn.execute(select(consultations).where(consultations.c.id==identifier,
            consultations.c.organization_id==user['organization_id'],consultations.c.user_id==user['id'])).mappings().first()
        if not row:raise WebError('NAO_ENCONTRADO',404)
        if row['status'] not in {'PENDENTE_LOTE','NA_FILA','EM_PROCESSAMENTO'}:return store.view(row,conn)
        saved=store.saved_payload(conn,user['organization_id'],row['cnpj'])
    if saved:
        with store.transaction() as conn:
            store.lock_org(conn,user['organization_id'])
            current=conn.execute(select(consultations).where(consultations.c.id==identifier)).mappings().one()
            if current['status'] in {'PENDENTE_LOTE','NA_FILA','EM_PROCESSAMENTO'}:
                payload=json.loads(saved);payload['origem_resultado']='HISTORICO'
                store.complete(conn,current,payload,reserved=True)
        return store.consultation(user['organization_id'],identifier)
    ensure_ready(store,key,user['organization_id'])
    outcome=runner(store,row['cnpj'],key)
    with store.transaction() as conn:
        store.lock_org(conn,user['organization_id'])
        row=conn.execute(select(consultations).where(consultations.c.id==identifier)).mappings().one()
        if row['status'] in {'PENDENTE_LOTE','NA_FILA','EM_PROCESSAMENTO'}:
            payload=outcome.get('result')
            if outcome['status']=='DONE':
                if not payload or payload.get('empresa',{}).get('cnpj')!=row['cnpj']:raise WebError('ERRO_TECNICO',500)
                packed=json.dumps(payload,ensure_ascii=False)
                # Never overwrite a previously saved higher quality company.
                store.upsert(conn,companies,dict(organization_id=user['organization_id'],cnpj=row['cnpj'],payload=packed,source_hash=digest(packed)),['organization_id','cnpj'])
                store.complete(conn,row,payload,reserved=True)
            elif outcome['status'] in {'FAILED','UNCERTAIN'}:
                conn.execute(update(organizations).where(organizations.c.id==user['organization_id']).values(reserved=organizations.c.reserved-1))
                conn.execute(update(consultations).where(consultations.c.id==identifier).values(status='FALHOU',
                    result=json.dumps(payload,ensure_ascii=False) if payload else None,finished_at=time.time()))
                conn.execute(update(batch_items).where(batch_items.c.consultation_id==identifier).values(error_code=outcome.get('error') or 'FALHA_TECNICA_PESQUISA'))
    data=store.consultation(user['organization_id'],identifier)
    data.update(etapa_pesquisa=outcome.get('phase'),retry_after=outcome.get('retry_after',0),erro=outcome.get('error'))
    return data
