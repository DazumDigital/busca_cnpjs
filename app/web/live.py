"""Executor HTTP retomável, com uma única chamada externa por etapa.

Estado, reservas e leases ficam no banco persistente. Etapas interrompidas
após reserva nunca são repetidas automaticamente, pois o provedor pode tê-las
recebido. Não há threads de trabalho duráveis ou fila em memória.
"""
import asyncio
import hmac
import json
import math
import time
import uuid
from sqlalchemy import case, func, select, insert, update
from app.config import policy_reason
from app.models import ProviderError
from app.services.importer import valid_cnpj
from app.web.store import (external_control, external_requests, live_jobs, consultations,
    companies, organizations, batch_items, WebError, digest)
from app.web.live_sources import execute_action, action_for, payload_for, stop_websites


PROVIDERS = ('serper', 'minhareceita', 'websites')
WINDOWS = (('per_minute', 60), ('per_hour', 3600), ('per_day', 86400))
MAX_TEMPORARY_RETRIES = 3


def valid_policy(policy):
    if not isinstance(policy, dict):
        return False
    orgs = policy.get('allowed_organizations')
    cnpjs = policy.get('allowed_cnpjs')
    if not isinstance(orgs, list) or not orgs or not all(isinstance(o, str) and o.strip() == o and o for o in orgs):
        return False
    if cnpjs is not None and (not isinstance(cnpjs, list) or not cnpjs or
            not all(isinstance(c, str) and len(c) == 14 and c.isascii() and c.isdecimal() and valid_cnpj(c) for c in cnpjs)):
        return False
    deadlines = policy.get('not_before', {})
    if not isinstance(deadlines, dict) or any(
            name not in PROVIDERS or type(t) not in (int, float) or not math.isfinite(t) or t < 0
            for name, t in deadlines.items()):
        return False
    for provider in PROVIDERS:
        limits = policy.get(provider)
        if not isinstance(limits, dict):
            return False
        if any(type(limits.get(k)) is not int or limits[k] <= 0 for k in ('max_requests', *(k for k, _ in WINDOWS))):
            return False
        interval = limits.get('min_interval', 0)
        if type(interval) not in (int, float) or not math.isfinite(interval) or not 0 <= interval <= 86400:
            return False
    try:
        return not policy_reason(policy['serper'], search=True)
    except AttributeError:
        return False


def checked_policy(control, key, organization=None, cnpjs=()):
    if not control or not key.strip():
        raise WebError('INTEGRACAO_NAO_VERIFICADA', 503)
    try:
        policy = json.loads(control['policy'])
    except (TypeError, ValueError):
        raise WebError('INTEGRACAO_NAO_VERIFICADA', 503) from None
    # Causas distintas, sem expor detalhes da política: suspensão pelo
    # responsável, configuração/confirmação pendente e escopo não liberado.
    if isinstance(policy, dict) and policy.get('blocked'):
        raise WebError('PESQUISA_SUSPENSA', 503)
    if (not valid_policy(policy) or not isinstance(policy.get('key_sha256'), str) or
            not hmac.compare_digest(policy['key_sha256'].encode(), digest(key).encode())):
        raise WebError('INTEGRACAO_NAO_VERIFICADA', 503)
    if (organization not in policy['allowed_organizations'] or
            ('allowed_cnpjs' in policy and policy['allowed_cnpjs'] is not None and
             any(cnpj not in policy['allowed_cnpjs'] for cnpj in cnpjs))):
        raise WebError('PESQUISA_NAO_LIBERADA', 403)
    return policy


def ensure_ready(store, key, organization, cnpjs=()):
    with store.engine.connect() as conn:
        row = conn.execute(select(external_control).where(external_control.c.id == 'global')).mappings().first()
        return checked_policy(row, key, organization, cnpjs)


def configure(store, policy, legacy=None, *, key):
    """Confirma a chave e a política sem zerar o histórico de consumo."""
    if not key.strip() or not valid_policy(policy):
        raise ValueError('Chave, escopo ou confirmação de cotas inválidos')
    policy = dict(policy, key_sha256=digest(key))
    seed = []
    if legacy:
        import sqlite3
        from pathlib import Path
        with sqlite3.connect(Path(legacy).resolve().as_uri() + '?mode=ro', uri=True) as db:
            seed = list(db.execute('SELECT id,provider,started_at,outcome,http_status FROM requests'))
    with store.transaction() as conn:
        store.upsert(conn, external_control, dict(id='global', policy=json.dumps(policy), lease_until=0), ['id'])
        control = conn.execute(select(external_control).where(external_control.c.id == 'global').with_for_update()).mappings().one()
        if control['lease_until'] > time.time():
            raise ValueError('Há uma etapa externa em execução')
        for identifier, provider, started, outcome, status in seed:
            name = f'poc_sqlite:{provider}:{identifier}'
            store.upsert(conn, external_requests, dict(id=name, provider=provider, started_at=started,
                outcome=outcome, http_status=status, operation=name), ['id'])
        previous = json.loads(control['policy'])
        # A reconfiguração não pode apagar um Retry-After ainda vigente.
        deadlines = dict(policy.get('not_before', {}))
        for provider, deadline in previous.get('not_before', {}).items():
            deadlines[provider] = max(deadlines.get(provider, 0), deadline)
        policy['not_before'] = deadlines
        conn.execute(update(external_control).where(external_control.c.id == 'global').values(policy=json.dumps(policy)))


def quota_delay(conn, control, policy, provider, now):
    p = policy[provider]
    started_at = external_requests.c.started_at
    request_times = select(started_at).where(external_requests.c.provider == provider)
    counts = [func.count(), func.max(started_at)]
    if provider == 'serper':
        from datetime import datetime
        since = datetime.fromisoformat(p['balance_verified_at']).timestamp()
        counts.append(func.count(case((started_at >= since, 1))))
    total, latest, *balance_usage = conn.execute(select(*counts).where(external_requests.c.provider == provider)).one()
    if total >= p['max_requests'] or (balance_usage and balance_usage[0] >= p['confirmed_free_remaining']):
        raise WebError('COTA_EXTERNA_ESGOTADA', 503)
    wait = max(0, control['lease_until'] - now, policy.get('not_before', {}).get(provider, 0) - now)
    for limit, seconds in WINDOWS:
        boundary = conn.execute(request_times.where(started_at > now - seconds)
            .order_by(started_at.desc()).offset(p[limit] - 1).limit(1)).scalar_one_or_none()
        if boundary is not None:
            wait = max(wait, boundary + seconds - now)
    if latest is not None:
        wait = max(wait, latest + p.get('min_interval', 0) - now)
    return max(0, math.ceil(wait))


def web_host(state):
    """Host do próximo acesso de validação (robots ou página), sem o alias www."""
    target=state.get('robots_url') or (state.get('queue') or [None])[0]
    if not isinstance(target,str):
        return None
    from urllib.parse import urlsplit
    return (urlsplit(target).hostname or '').removeprefix('www.') or None


def advance_job(store,cnpj,key,clock=time.time,executor=execute_action,*,organization):
    with store.transaction() as conn:
        control=conn.execute(select(external_control).where(external_control.c.id=='global').with_for_update()).mappings().first()
        now=clock()
        policy=checked_policy(control,key,organization,(cnpj,))
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
        try:
            wait=quota_delay(conn,control,policy,provider,now)
        except WebError as exc:
            if provider!='websites' or exc.code!='COTA_EXTERNA_ESGOTADA':
                raise
            # Sem cota para validar páginas: conclui localmente com o cadastro e a
            # busca já pagos, sem nova chamada e sem perder o crédito da Serper.
            state=stop_websites(state)
            result=payload_for(state)
            conn.execute(update(live_jobs).where(live_jobs.c.cnpj==cnpj).values(state=json.dumps(state,ensure_ascii=False),
                status='DONE',step=job['step']+1,lease_until=0,result=json.dumps(result,ensure_ascii=False),error=None,updated_at=now))
            return dict(status='DONE',result=result,error=None,phase='DONE',retry_after=0)
        if provider=='minhareceita':
            # Evita até a chamada cadastral quando a etapa Serper está sem orçamento.
            wait=max(wait,quota_delay(conn,control,policy,'serper',now))
        # Crawl-delay vale por host (RFC 9309), não para todos os sites: pesquisas
        # de empresas diferentes avançam em paralelo; o mesmo host espera.
        host=web_host(state) if provider=='websites' else None
        if host:
            wait=max(wait,math.ceil(policy.get('host_not_before',{}).get(host,0)-now))
        if wait>0:return dict(status='WAIT',retry_after=wait,phase=state['phase'])
        operation=job['id']+':'+str(job['step'])
        lease_until = now + 40
        conn.execute(insert(external_requests).values(id=str(uuid.uuid4()),provider=provider,started_at=now,outcome='RESERVADA',operation=operation))
        conn.execute(update(external_control).where(external_control.c.id=='global').values(lease_until=lease_until))
        conn.execute(update(live_jobs).where(live_jobs.c.cnpj==cnpj).values(status='IN_FLIGHT',lease_until=lease_until,updated_at=now))
    error=None; result=None;cooldown=0;paused=False
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
        # HTTP 429 means the provider refused before processing: keep the phase
        # and wait for the cooldown, a bounded number of times, never in a loop.
        if error=='LIMITE_TEMPORARIO' and state.get('temporary_retries',0)<MAX_TEMPORARY_RETRIES:
            paused=True;cooldown=max(cooldown,60)
            state=dict(state,temporary_retries=state.get('temporary_retries',0)+1)
        elif state.get('company'):
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
        if host:
            moment=clock()
            hosts={h:t for h,t in policy.get('host_not_before',{}).items() if type(t) in (int,float) and t>moment}
            hosts[host]=moment+max(10,state.get('crawl_delay',10))
            policy['host_not_before']=hosts
        if cooldown:
            deadlines=dict(policy.get('not_before',{}));deadlines[provider]=clock()+cooldown;policy['not_before']=deadlines
        conn.execute(update(external_requests).where(external_requests.c.operation==operation).values(outcome='FALHA' if error else 'SUCESSO'))
        conn.execute(update(external_control).where(external_control.c.id=='global').values(lease_until=0,policy=json.dumps(policy)))
        status='READY' if paused else 'FAILED' if error else 'DONE' if done else 'READY'
        conn.execute(update(live_jobs).where(live_jobs.c.cnpj==cnpj).values(state=json.dumps(state,ensure_ascii=False),
            status=status,step=job['step']+1,lease_until=0,
            result=json.dumps(result,ensure_ascii=False) if result else None,error=None if paused else error,updated_at=clock()))
    if paused:
        return dict(status='WAIT',retry_after=math.ceil(cooldown),phase=state['phase'])
    return dict(status=status,result=result,error=error,phase=state['phase'],retry_after=0)


def block(store):
    """Desligamento imediato pelo banco, sem redeploy; reativar exige configure."""
    with store.transaction() as conn:
        control=conn.execute(select(external_control).where(external_control.c.id=='global').with_for_update()).mappings().first()
        if not control:
            return False
        policy=json.loads(control['policy']);policy['blocked']=True
        conn.execute(update(external_control).where(external_control.c.id=='global').values(policy=json.dumps(policy)))
        return True


def external_status(store):
    """Resumo auditável, sem hash de chave nem dados das empresas."""
    with store.engine.connect() as conn:
        control=conn.execute(select(external_control).where(external_control.c.id=='global')).mappings().first()
        policy=json.loads(control['policy']) if control else None
        if policy:
            policy={k:v for k,v in policy.items() if k!='key_sha256'}
        ledger=conn.execute(select(external_requests.c.provider,external_requests.c.outcome,func.count())
            .group_by(external_requests.c.provider,external_requests.c.outcome)).all()
        jobs=conn.execute(select(live_jobs.c.status,func.count()).group_by(live_jobs.c.status)).all()
    return dict(politica=policy,lease_ativo=bool(control and control['lease_until']>time.time()),
        livro_tecnico=[dict(provedor=p,resultado=o,quantidade=n) for p,o,n in ledger],
        pesquisas=dict((s,n) for s,n in jobs))


def restart_websites(store,cnpj,clock=time.time):
    """Refaz somente a validação de sites de uma pesquisa concluída, com os
    candidatos já obtidos. Não reserva Serper nem cadastro; o histórico da
    rodada anterior fica no estado e o livro técnico não é alterado."""
    from app.models import Company
    from app.web.live_sources import candidate_sites
    with store.transaction() as conn:
        control=conn.execute(select(external_control).where(external_control.c.id=='global').with_for_update()).mappings().first()
        if control and control['lease_until']>clock():
            raise ValueError('Há uma etapa externa em execução')
        job=conn.execute(select(live_jobs).where(live_jobs.c.cnpj==cnpj).with_for_update()).mappings().first()
        if not job or job['status']!='DONE':
            raise ValueError('Só uma pesquisa concluída pode ter os sites revalidados')
        state=json.loads(job['state'])
        if not state.get('company') or not state.get('searched_at'):
            raise ValueError('Pesquisa sem busca registrada')
        sites=candidate_sites(Company(**state['company']),cnpj,state.get('candidates',[]),first=state.get('google_panel_site'))
        if not sites:
            raise ValueError('Nenhum candidato a site para validar')
        runs=state.get('website_runs',[])+[dict(finished_at=job['updated_at'],web_calls=state.get('web_calls',0),
            site_failures=state.get('site_failures',[]),site_type=(state.get('validation') or {}).get('site_type'))]
        for name in ('validation','site_results','site_failures','page_failures','robots','robots_status','robots_url',
                     'robots_redirects','validations','site_validation','crawl_delay'):
            state.pop(name,None)
        state.update(phase='WEBSITE',queue=sites[:1],sites=sites[1:],current_site=sites[0],site_results=[],site_failures=[],
                     web_calls=0,robots={},validations=[],redirects=[],website_runs=runs,revalidation=True)
        conn.execute(update(live_jobs).where(live_jobs.c.cnpj==cnpj).values(status='READY',error=None,result=None,
            lease_until=0,step=job['step']+1,state=json.dumps(state,ensure_ascii=False),updated_at=clock()))
        return dict(sites=sites,rodadas_anteriores=len(runs))


def release_job(store,cnpj,clock=time.time):
    """Liberação administrativa, após revisar a auditoria, de uma pesquisa falha
    ou incerta. Mantém a fase e o livro técnico; a próxima etapa é nova."""
    with store.transaction() as conn:
        control=conn.execute(select(external_control).where(external_control.c.id=='global').with_for_update()).mappings().first()
        if control and control['lease_until']>clock():
            raise ValueError('Há uma etapa externa em execução')
        job=conn.execute(select(live_jobs).where(live_jobs.c.cnpj==cnpj).with_for_update()).mappings().first()
        if not job or job['status'] not in {'FAILED','UNCERTAIN'}:
            raise ValueError('Não há pesquisa falha ou incerta para este CNPJ')
        state=json.loads(job['state']);state.pop('temporary_retries',None)
        # The step counter advances so a new reservation never reuses the
        # operation id of the uncertain call.
        conn.execute(update(live_jobs).where(live_jobs.c.cnpj==cnpj).values(status='READY',error=None,result=None,
            lease_until=0,step=job['step']+1,state=json.dumps(state,ensure_ascii=False),updated_at=clock()))
        return dict(anterior=job['status'],erro=job['error'],fase=state['phase'])


def revalidate_consultation(store,user,row,key,runner):
    """Avança a revalidação de sites de uma consulta já concluída e cobrada.
    Atualiza o resultado da própria consulta e da organização, sem novo token."""
    organization=user['organization_id']
    outcome=runner(store,row['cnpj'],key,organization=organization)
    payload=outcome.get('result')
    if outcome['status']=='DONE':
        if not payload or payload.get('empresa',{}).get('cnpj')!=row['cnpj']:
            raise WebError('ERRO_TECNICO',500)
        payload=dict(payload,ressalvas=list(payload.get('ressalvas',[]))+[
            'Sites revalidados com os links já encontrados, sem nova busca paga e sem novo desconto de token.'])
        packed=json.dumps(payload,ensure_ascii=False)
        with store.transaction() as conn:
            store.lock_org(conn,organization)
            current=conn.execute(select(consultations).where(consultations.c.id==row['id'])).mappings().one()
            if current['status']=='CONCLUIDA':
                store.upsert(conn,companies,dict(organization_id=organization,cnpj=row['cnpj'],payload=packed,source_hash=digest(packed)),
                    ['organization_id','cnpj'],dict(payload=packed,source_hash=digest(packed)))
                conn.execute(update(consultations).where(consultations.c.id==row['id']).values(result=packed))
    data=store.consultation(organization,row['id'])
    data.update(etapa_pesquisa=outcome.get('phase'),retry_after=outcome.get('retry_after',0),erro=outcome.get('error'))
    return data


def process_consultation(store,user,identifier,key,runner=advance_job):
    with store.engine.connect() as conn:
        row=conn.execute(select(consultations).where(consultations.c.id==identifier,
            consultations.c.organization_id==user['organization_id'],consultations.c.user_id==user['id'])).mappings().first()
        if not row:raise WebError('NAO_ENCONTRADO',404)
        saved=store.saved_payload(conn,user['organization_id'],row['cnpj'])
        if row['status']=='CONCLUIDA' and saved and store.revalidation_pending(conn,row['cnpj']):
            revalidate=True
        elif row['status'] not in {'PENDENTE_LOTE','NA_FILA','EM_PROCESSAMENTO'}:
            return store.view(row,conn)
        else:
            revalidate=False
    if revalidate:
        return revalidate_consultation(store,user,row,key,runner)
    if saved:
        with store.transaction() as conn:
            store.lock_org(conn,user['organization_id'])
            current=conn.execute(select(consultations).where(consultations.c.id==identifier)).mappings().one()
            if current['status'] in {'PENDENTE_LOTE','NA_FILA','EM_PROCESSAMENTO'}:
                payload=json.loads(saved);payload['origem_resultado']='HISTORICO'
                if payload.get('empresa',{}).get('cnpj') != current['cnpj']:
                    raise WebError('ERRO_TECNICO',500)
                # Já salvo (ex.: pesquisado por outra consulta da organização):
                # reaproveita sem nova busca e devolve a reserva.
                store.complete(conn,current,payload,reserved=True,charge=False)
        return store.consultation(user['organization_id'],identifier)
    try:
        outcome=runner(store,row['cnpj'],key,organization=user['organization_id'])
    except WebError as exc:
        if exc.code != 'COTA_EXTERNA_ESGOTADA':
            raise
        outcome=dict(status='FAILED',error=exc.code)
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
    data.update(etapa_pesquisa=outcome.get('phase'),retry_after=outcome.get('retry_after',0),erro=outcome.get('error') or data.get('erro'))
    return data
