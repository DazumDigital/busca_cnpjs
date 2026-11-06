"""Prepara e inspeciona offline; somente --run executa o lote futuro autorizado."""
import argparse
import asyncio
import csv
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from app.config import Settings, load_policies, policy_reason
from app.models import Company
from app.reclassify import reclassify
from app.reports.exporter import export_all, make_report
from app.services.importer import import_file, normalize_cnpj, valid_cnpj
from app.services.search_queries import queries_for
from app.services.site_rules import rank_candidates, url_reason
from app.services.validator import safe_url
from app.storage.database import Database
from app.storage.lock import ProcessLock


def small_settings(settings):
    return replace(settings, max_companies=min(3, settings.max_companies), batch_size=3, concurrency=1,
                   queries=min(2, settings.queries), retries=0, enable_brasilapi=False,
                   website_max_requests=min(120, settings.website_max_requests),
                   website_rpm=min(6, settings.website_rpm), website_delay=max(5, settings.website_delay))


def readonly(path):
    return closing(sqlite3.connect(f"file:{Path(path).resolve().as_posix()}?mode=ro", uri=True))


def budget_status(source, policies, settings, now=None):
    now = time.time() if now is None else now
    with readonly(source) as conn:
        rows = conn.execute('SELECT provider,started_at,outcome FROM requests').fetchall()
    result = {}
    configs = dict(policies)
    configs['websites'] = dict(max_requests=settings.website_max_requests, per_minute=settings.website_rpm,
                              per_hour=settings.website_rpm*60, per_day=settings.website_max_requests)
    for name in ('serper', 'websites'):
        policy = configs.get(name, {})
        stamps = [r[1] for r in rows if r[0] == name]
        cap = policy.get('max_requests', 0)
        info = dict(usadas=len(stamps), teto_local=cap, restantes_locais=max(0, cap-len(stamps)))
        for label, seconds in (('minuto',60),('hora',3600),('dia',86400)):
            used = sum(t > now-seconds for t in stamps)
            info['restantes_'+label] = max(0, policy.get('per_'+{'minuto':'minute','hora':'hour','dia':'day'}[label],0)-used)
        if name == 'serper':
            balance = policy.get('confirmed_free_remaining')
            at = policy.get('balance_verified_at')
            estimate = None
            try:
                snapshot = datetime.fromisoformat(at).timestamp()
                if type(balance) is int:
                    estimate = max(0, balance-sum(t >= snapshot for t in stamps))
            except (TypeError, ValueError):
                pass
            info.update(saldo_informado_no_manifesto=balance, horario_snapshot=at,
                        estimativa_apos_tentativas_desta_aplicacao=estimate,
                        saldo_atual_provedor='NAO_CONSULTADO',
                        pendencia_verificacao=policy_reason(policy, search=True))
        result[name] = info
    return result


def prepare(source, output, ids, policies, settings):
    settings = small_settings(settings)
    source, output = Path(source).resolve(), Path(output).resolve()
    ids = [normalize_cnpj(c) for c in ids]
    if not 1 <= len(ids) <= 3 or len(set(ids)) != len(ids) or not all(valid_cnpj(c) for c in ids):
        raise ValueError('Selecione de um a três CNPJs válidos e distintos')
    with readonly(source) as conn:
        conn.row_factory = sqlite3.Row
        selected = [conn.execute('SELECT * FROM companies WHERE cnpj=?',(c,)).fetchone() for c in ids]
        if any(r is None for r in selected):
            raise ValueError('Todos os CNPJs precisam existir na base atual')
        companies = [Company(**json.loads(r['company'] or '{}')) for r in selected]
        if any(not (c.legal_name or c.trade_name) for c in companies):
            raise ValueError('O lote de descoberta requer identidade empresarial local')
        cached_domains = set()
        for value, in conn.execute("SELECT value FROM cache WHERE provider='validation'"):
            try:
                cached_domains.add(urlsplit(json.loads(value).get('website','')).hostname)
            except (ValueError,AttributeError):
                continue
        analysis=[]
        for company,row in zip(companies,selected):
            candidates=json.loads(row['candidates'] or '[]')
            eligible=[]
            for url in candidates:
                try:
                    clean,_,_=safe_url(url)
                    if not url_reason(clean,company.cnpj): eligible.append(clean)
                except ValueError:
                    continue
            analysis.append(dict(cnpj=company.cnpj,total_candidates=len(candidates),
                                 corporate_candidates=rank_candidates(company,eligible),
                                 rejected_before_validation=len(candidates)-len(eligible),
                                 corporate_candidates_with_legacy_validation=sum(urlsplit(u).hostname in cached_domains for u in eligible)))
    # O reclassificador protege a origem e exige uma saída nova.
    reclassify(source, output, 10)
    current_budget = budget_status(source, policies, settings)
    prepared_input = output / 'entrada_poc3.csv'
    with prepared_input.open('w',encoding='utf-8-sig',newline='') as stream:
        writer = csv.writer(stream,delimiter=';')
        writer.writerow(['CNPJ','razao_social','nome_fantasia','municipio','UF','situacao_cadastral'])
        writer.writerows([c.cnpj,c.legal_name,c.trade_name,c.city,c.state,c.registration_status] for c in companies)
    initial = {name: current_budget[name]['usadas'] for name in current_budget}
    plan = dict(mode='PREPARACAO_OFFLINE_NAO_EXECUTADA', created_at=datetime.now(timezone.utc).isoformat(),
                source_database=str(source), working_database=str(output/'reclassificacao.sqlite'),
                input=str(prepared_input), output=str(output/'execucao'), ids=ids,
                limits=dict(companies=3, concurrency=1, queries_per_company=settings.queries,
                            retries=0, serper_new_attempts=6, website_new_attempts=40),
                initial_usage=initial, budget=current_budget,
                candidate_analysis=analysis,
                queries=[dict(cnpj=c.cnpj,company=c.legal_name,queries=queries_for(c)[:settings.queries]) for c in companies])
    (output/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
    return plan


def read_plan(path):
    plan = json.loads(Path(path).read_text(encoding='utf-8'))
    ids = plan['ids']
    if not 1 <= len(ids) <= 3 or len(set(ids)) != len(ids) or not all(valid_cnpj(c) for c in ids):
        raise ValueError('Plano inválido ou acima de três empresas')
    if Path(plan['source_database']).resolve() == Path(plan['working_database']).resolve():
        raise ValueError('Banco de trabalho deve ser separado dos checkpoints originais')
    return plan


async def execute_plan(plan, settings, policies, *, dry_run):
    settings = small_settings(settings)
    # Importações HTTP ficam dentro do ramo real, nunca na preparação/checagem.
    input_data = import_file(Path(plan['input']), 3)
    ids = [c for c,_ in input_data.selected]
    if ids != plan['ids']:
        raise ValueError('Entrada difere dos CNPJs selecionados; prepare um novo plano')
    started = time.time()
    db = Database(Path(plan['working_database']))
    ledger = None
    try:
        db.ingest(input_data)
        if dry_run:
            from app.services.quota_manager import QuotaManager
            quota = QuotaManager(db,{})
            report = make_report(db,ids,input_data,quota,started,'PILOT3_DRY_RUN',0)
            report['orcamento_compartilhado'] = budget_status(plan['source_database'],policies,settings)
            export_all(Path(plan['output']).parent/'dry-run3',db,ids,input_data,report)
            return report
        reason = policy_reason(policies.get('serper',{}),search=True)
        if reason or not settings.enable_serper or not settings.serper_key:
            raise ValueError('Serper não liberada: confira flags, chave, conta gratuita e manifesto; '+reason)
        import httpx
        from app.providers.base import JsonAPI
        from app.providers.search import Serper
        from app.security import tls_context
        from app.services.enrichment import Enrichment
        from app.services.processor import Processor
        from app.services.quota_manager import QuotaManager
        from app.services.validator import SafeFetcher, WebsiteValidator
        from app.services.website_finder import WebsiteFinder
        ledger = Database(Path(plan['source_database']))
        # Não reiniciar contadores numa cópia. Consumo/cache/bloqueios são comuns
        # ao banco original; somente os checkpoints de empresas ficam separados.
        serper_policy = dict(policies['serper'])
        serper_policy['max_requests'] = min(serper_policy['max_requests'],plan['initial_usage']['serper']+2*len(ids))
        web_cap = min(settings.website_max_requests,plan['initial_usage']['websites']+40)
        active = {'serper':serper_policy,'websites':dict(max_requests=web_cap,per_minute=settings.website_rpm,
                   per_hour=settings.website_rpm*60,per_day=settings.website_max_requests)}
        quota = QuotaManager(ledger,active)
        # Bloquear antes de construir clientes se o orçamento desse plano acabou.
        usage = ledger.usage()
        if usage.get('serper',{}).get('requisicoes',0) >= serper_policy['max_requests']:
            raise ValueError('Orçamento local do plano/cota global esgotado; não reiniciar por outra cópia')
        if usage.get('websites',{}).get('requisicoes',0) >= web_cap:
            raise ValueError('Cota local de websites esgotada; não consumir busca sem poder validar')
        if ledger.get_state('provider-block:serper'):
            raise ValueError('Provedor bloqueado; corrigir a causa sem reiniciar contadores')
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False,verify=tls_context(),
                                     headers={'User-Agent':'DazumCNPJ-POC/0.1'}) as client:
            search = Serper(JsonAPI('serper',client,quota,ledger,settings),settings.serper_key)
            finder = WebsiteFinder(search,WebsiteValidator(SafeFetcher(quota,settings),ledger,settings),db,settings)
            processor = Processor(db,Enrichment(None),finder,settings)
            interrupted = False
            try:
                await processor.run(ids,retry=True)
            except asyncio.CancelledError:
                interrupted = True
                raise
            finally:
                report = make_report(db,ids,input_data,quota,started,'PILOT3_REAL',processor.completed_this_run,interrupted)
                report['requisicoes_nesta_execucao'] = ledger.usage(started)
                report['requisicoes_acumuladas'] = ledger.usage()
                report['custo_adicional_efetivo_usd'] = None if ledger.usage(started) else 0
                report['custo_verificacao'] = 'Consultar painel do provedor; orçamento local não comprova faturamento'
                report['banco_compartilhado_de_cotas'] = plan['source_database']
                export_all(Path(plan['output']),db,ids,input_data,report)
        return report
    finally:
        if ledger: ledger.close()
        db.close()


def main(argv=None):
    cli = argparse.ArgumentParser(description='POC de até três empresas; preparar/checar/dry-run não consultam provedores')
    modes = cli.add_mutually_exclusive_group(required=True)
    modes.add_argument('--prepare',action='store_true')
    modes.add_argument('--check',action='store_true')
    modes.add_argument('--dry-run',action='store_true')
    modes.add_argument('--run',action='store_true',help='Execução real: usar somente após autorização explícita')
    cli.add_argument('--source-database',type=Path,default=Path('data/poc.sqlite'))
    cli.add_argument('--output',type=Path)
    cli.add_argument('--cnpjs',nargs='+')
    cli.add_argument('--plan',type=Path)
    cli.add_argument('--env-file',type=Path,default=Path('.env'))
    cli.add_argument('--policies',type=Path,default=Path('provider_verification.json'))
    args=cli.parse_args(argv)
    try:
        settings=small_settings(Settings.from_env(args.env_file))
        policies=load_policies(args.policies)
        if args.prepare:
            if not args.output or not args.cnpjs:
                raise ValueError('--prepare exige --output novo e --cnpjs (máximo 3)')
            plan=prepare(args.source_database,args.output,args.cnpjs,policies,settings)
            print(json.dumps(dict(plano=str(args.output/'plan.json'),ids=plan['ids'],budget=plan['budget'],
                                  limits=plan['limits'],mode=plan['mode']),ensure_ascii=False,indent=2))
        else:
            if not args.plan: raise ValueError('Informe --plan')
            plan=read_plan(args.plan)
            if args.check:
                print(json.dumps(dict(ids=plan['ids'],limits=plan['limits'],queries=plan['queries'],
                                      candidate_analysis=plan['candidate_analysis'],
                                      configuracao=dict(serper_flag=settings.enable_serper,
                                                        chave_presente=bool(settings.serper_key),
                                                        manifesto_valido=not bool(policy_reason(policies.get('serper',{}),search=True)),
                                                        brasilapi_habilitada=False),
                                      budget=budget_status(plan['source_database'],policies,settings),
                                      chamadas_externas=0),ensure_ascii=False,indent=2))
            else:
                with ProcessLock(Path(plan['working_database']).with_suffix('.run.lock')):
                    if args.run:
                        with ProcessLock(Path(plan['source_database']).with_suffix('.run.lock')):
                            result=asyncio.run(execute_plan(plan,settings,policies,dry_run=False))
                    else:
                        result=asyncio.run(execute_plan(plan,settings,policies,dry_run=True))
                print(json.dumps(dict(mode=result['modo'],companies=len(plan['ids'])),ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        print('Interrompido; checkpoints, evidências e consumo preservados')
        return 130
    except (ValueError,OSError,sqlite3.Error,KeyError) as error:
        print('Preparação/execução interrompida: '+str(error))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
