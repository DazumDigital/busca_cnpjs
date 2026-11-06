"""Separa o website oficial de candidatos, ausência de evidência e falhas técnicas."""
from dataclasses import replace

from app.services.site_rules import RULES_VERSION, url_reason
from app.services.validator import safe_url


def official_website(value):
    """Campo público exige vínculo confirmado, além de uma URL elegível."""
    if not (value.get('website') and value.get('confirmed') is True and
            value.get('confidence') == 'ALTA' and value.get('site_type') == 'CORPORATE'):
        return ''
    if not all(value.get('signals',{}).get(k) is True for k in
               ('exact_cnpj','name_match','legal_owner','heading_brand')):
        return ''
    signals=value.get('signals',{})
    if signals.get('brand_domain') is not True and not (
            signals.get('tenant_identity_confirmed') is True and len(set(signals.get('tenant_evidence_paths',[])))>=2):
        return ''
    try:
        clean, _, _ = safe_url(value['website'])
    except ValueError:
        return ''
    return '' if url_reason(clean) else clean


def public_validation(result):
    """Preserva a URL/evidência para revisão, sem publicá-la como site oficial."""
    if official_website(result.to_dict()):
        return result
    signals = dict(result.signals)
    kind, confidence = result.site_type, result.confidence
    evidence = list(result.evidence)
    if result.website:
        signals['review_candidate_url'] = result.website
        try:
            clean, _, _ = safe_url(result.website)
            reason = url_reason(clean)
        except ValueError:
            reason = 'URL_INSEGURA'
        if reason or kind in {'THIRD_PARTY', 'CNPJ_DIVERGENTE'}:
            kind, confidence = 'THIRD_PARTY_ONLY', 'NAO_ENCONTRADO'
            evidence.append(reason or 'Candidato de terceiro ou identidade divergente')
        else:
            signals['candidate_url'] = result.website
            kind = 'CORPORATE_PENDING'
            if confidence not in {'BAIXA','MEDIA','PENDENTE'}:
                confidence = 'PENDENTE'
    if confidence == 'ALTA':
        confidence = 'PENDENTE'
        if kind == 'CORPORATE':kind = 'CORPORATE_PENDING'
    return replace(result, website='', domain='', confirmed=False, signals=signals,
                   site_type=kind, confidence=confidence, evidence=evidence, rules_version=RULES_VERSION)


def result_status(result):
    if official_website(result.to_dict()):
        return 'CONCLUIDO'
    if result.site_type == 'VALIDATION_ERROR':
        return 'FALHA_VALIDACAO_SITE_OFICIAL'
    if result.site_type in {'THIRD_PARTY_ONLY', 'THIRD_PARTY', 'CNPJ_DIVERGENTE'}:
        return 'SEM_SITE_OFICIAL_CONFIRMADO'
    if result.site_type == 'CORPORATE_PENDING' or result.confidence in {'MEDIA', 'BAIXA', 'PENDENTE'}:
        return 'PENDENTE_VALIDACAO_SITE_OFICIAL'
    return 'SEM_SITE_OFICIAL_CONFIRMADO'
