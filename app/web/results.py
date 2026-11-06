"""Contrato público: lista permitida de campos, sem auditoria técnica ou caminhos locais."""
from urllib.parse import urlsplit
import re

from app.models import Company, Validation, UnsafeURL
from app.services.site_outcome import public_validation
from app.services.site_rules import candidate_kind, brand_tokens, folded
from app.services.validator import safe_url


def public_url(value):
    try:
        return safe_url(value)[0]
    except (UnsafeURL, TypeError):
        return ''


def page_evidence(facts):
    evidence = []
    for fact in facts:
        url = public_url(fact.get('url', ''))
        if url:
            evidence.append(dict(url=url, titulo=str(fact.get('title', ''))[:400],
                trecho=re.sub(r'\s+', ' ', fact.get('owner_context', '')).strip()[:500],
                cnpjs=fact.get('cnpjs', []), verificado_em=fact.get('reviewed_at_utc')))
    return evidence


def link(url, kind, confidence, explanation, **extra):
    clean = public_url(url)
    return dict(url=clean, dominio=urlsplit(clean).hostname, tipo=kind, confianca=confidence, evidencia=explanation, **extra) if clean else None


def corresponding_social(url, company):
    # Search results can include unrelated people or generic posts. A profile handle
    # must at least carry a distinctive business token before it is shown publicly.
    path = urlsplit(url).path.lower()
    if re.match(r'^/(p|reel|reels|stories|share|search|explore)(/|$)', path):
        return False
    compact = folded(path).replace(' ', '')
    return any(token in compact for token in brand_tokens(Company(**company)))


def public_result(record, company, validation, references, manual=None):
    validated = public_validation(Validation(**validation))
    official = link(validated.website, 'CONFIRMADO_AUTOMATICAMENTE', 'ALTA',
                    'Titularidade confirmada com evidências institucionais armazenadas.',
                    paginas=page_evidence(validated.page_evidence)) if validated.website else None
    check = manual or {}
    manual_link = link(check.get('url') or record.get('manual_website', ''), 'VERIFICACAO_MANUAL_INFORMADA', 'MANUAL',
                       check.get('evidence') or record.get('manual_evidence', ''),
                       ressalvas=['Verificação informada pelo usuário; não revalidada automaticamente.'])
    pending = record.get('pending_candidate', '') or validated.signals.get('candidate_url', '')
    candidates = []
    if pending and not official and candidate_kind(pending) == 'UNVERIFIED_CANDIDATE':
        item = link(pending, 'POSSIVEL_SITE', validated.confidence,
                    'A validação técnica não pôde ser concluída; o vínculo ainda não foi avaliado.' if validated.site_type == 'VALIDATION_ERROR' else
                    'As evidências armazenadas ainda não confirmam a titularidade deste site.')
        if item:
            candidates.append(item)
    socials = []
    for url in record.get('social_candidates', []):
        if candidate_kind(url) == 'SOCIAL_NETWORK' and corresponding_social(url, company):
            item = link(url, 'REDE_SOCIAL', 'NAO_CONFIRMADO', 'Perfil encontrado; vínculo com a empresa não confirmado.')
            if item:
                socials.append(item)
    refs = []
    for reference in references:
        item = link(reference['url'], reference['source_type'], reference['confidence'], reference['evidence'],
                    ressalvas=reference.get('alerts', []), revisao_necessaria=reference.get('needs_review', False),
                    acesso_atual_verificado=False)
        if item:
            refs.append(item)
    return dict(empresa=dict(cnpj=company['cnpj'], razao_social=company.get('legal_name', ''),
        nome_fantasia=company.get('trade_name', ''), municipio=company.get('city', ''), uf=company.get('state', ''),
        situacao_cadastral=company.get('registration_status', ''), pesquisado_em=record.get('source_updated_at')),
        website_oficial=official, verificacao_manual=manual_link, possiveis_sites=candidates,
        redes_sociais=socials, referencias=refs, status=record.get('automatic_status', 'SEM_SITE_OFICIAL_CONFIRMADO'),
        ressalvas=['Resultados históricos. A disponibilidade atual dos links não foi verificada nesta consulta.'])
