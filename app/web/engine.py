"""Adaptador local explícito para o motor existente; nunca iniciado pela função HTTP."""
import asyncio
import json
from dataclasses import replace

import httpx

from app.config import Settings, load_policies
from app.main import provider_status
from app.pilot import pilot_policies
from app.pilot3 import small_settings
from app.new_batch import researched_ids
from app.models import Company
from app.providers.base import JsonAPI
from app.providers.search import Serper
from app.security import tls_context
from app.services.importer import ImportResult
from app.services.quota_manager import QuotaManager
from app.services.references import useful_references
from app.services.site_outcome import public_validation, result_status
from app.services.site_rules import candidate_groups, requires_reclassification
from app.services.validator import SafeFetcher, WebsiteValidator
from app.services.website_finder import WebsiteFinder
from app.storage.database import Database
from app.storage.lock import ProcessLock
from app.web.config import ROOT
from app.web.results import public_result


def external_settings(settings):
    return replace(small_settings(settings), concurrency=1, retries=0, candidates=1, queries=1, max_companies=1, batch_size=1)


async def research_one(cnpj):
    # Paths are fixed server-side. No URL, credential or ledger location from HTTP.
    settings = external_settings(Settings.from_env(ROOT / '.env'))
    policies = pilot_policies(load_policies(ROOT / 'provider_verification.json'))
    state = provider_status(settings, policies)
    if not state['serper']['habilitado']:
        raise ValueError('Integração não autorizada ou verificação expirada')
    ledger = ROOT / 'data/poc.sqlite'
    if not ledger.exists():
        raise ValueError('O livro de consumo canônico é obrigatório; não criar um contador novo')
    checkpoint = ROOT / 'var/engine.sqlite'
    with ProcessLock(ledger.with_suffix('.run.lock')), ProcessLock(checkpoint.with_suffix('.run.lock')):
        if cnpj in researched_ids(ROOT / 'data'):
            raise ValueError('CNPJ já pesquisado: importar/revisar os dados existentes, sem nova pesquisa')
        db = Database(ledger)
        work = None
        try:
            usage = db.usage()
            web_used = usage.get('websites', {}).get('requisicoes', 0)
            search_used = usage.get('serper', {}).get('requisicoes', 0)
            web_cap = min(settings.website_max_requests, web_used + 12)
            search_policy = dict(policies['serper'], max_requests=min(policies['serper']['max_requests'], search_used+1))
            if web_used >= web_cap or search_used >= search_policy['max_requests'] or db.get_state('provider-block:serper'):
                raise ValueError('Cota esgotada ou provedor bloqueado; não iniciar a pesquisa')
            work = Database(checkpoint)
            if work.row(cnpj):
                raise ValueError('Checkpoint já existe; revisão manual necessária antes de nova pesquisa')
            work.ingest(ImportResult([(cnpj, cnpj)], [], {'selecionados': 1}))
            quota = QuotaManager(db, dict(serper=search_policy, websites=dict(max_requests=web_cap,
                per_minute=settings.website_rpm, per_hour=settings.website_rpm*60, per_day=settings.website_max_requests)))
            company = Company(cnpj)
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, verify=tls_context(), headers={'User-Agent': 'DazumCNPJ-POC/0.1'}) as client:
                search = Serper(JsonAPI('serper', client, quota, db, settings), settings.serper_key)
                finder = WebsiteFinder(search, WebsiteValidator(SafeFetcher(quota, settings), db, settings), work, settings)
                result, candidates, errors = await finder.find(company)
            validated = public_validation(result)
            if errors or validated.site_type == 'VALIDATION_ERROR':
                raise ValueError('Pesquisa não concluída com segurança')
            refs = useful_references(company, candidates, validated.page_evidence, validated.website)
            record = dict(cnpj=cnpj, automatic_status=result_status(validated),
                          social_candidates=candidate_groups(candidates)['social_networks'])
            payload = public_result(record, company.__dict__, validated.to_dict(), refs)
            work.update(cnpj, validation=validated.to_dict(), candidates=candidates, confidence=validated.confidence,
                      status=result_status(validated), source='serper', error='')
            return payload
        finally:
            if work:
                work.close()
            db.close()
