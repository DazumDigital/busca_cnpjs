"""Referências úteis são vínculos de informação, nunca confirmação de titularidade."""
import re
from urllib.parse import unquote, urlsplit

from app.services.site_rules import candidate_kind, extract_cnpjs, folded
from app.services.validator import safe_url


def identity_tokens(name):
    stop={'ltda','limitada','eireli','me','epp','sa','s','a','e','de','da','do','dos','das'}
    return [t for t in folded(name).split() if t not in stop]


def name_matches(company,text):
    words=' '+folded(text)+' '
    return any(len(tokens)>=2 and all(' '+t+' ' in words for t in tokens) for tokens in
               (identity_tokens(company.legal_name),identity_tokens(company.trade_name)))


def useful_references(company, urls, facts=(), official_url=''):
    selected=[]
    for raw in dict.fromkeys(urls):
        try: url,_,_=safe_url(raw)
        except ValueError: continue
        if url==official_url:continue
        parts=urlsplit(url);path=unquote(parts.path)
        numbers=extract_cnpjs(path+' '+parts.query)
        # Inscrição divergente não vira referência por coincidir com o nome.
        if numbers and numbers!={company.cnpj}:continue
        saved=[f for f in facts if f.get('url')==url and f.get('source')=='PUBLIC_PAGE_DERIVED_EVIDENCE'
               and re.fullmatch(r'[0-9a-f]{64}',f.get('content_sha256',''))]
        saved_numbers=set().union(*(set(f.get('cnpjs',[])) for f in saved)) if saved else set()
        if saved_numbers and company.cnpj not in saved_numbers:continue
        titles=' '.join(f.get('title','') for f in saved)
        context=' '.join(f.get('owner_context','') for f in saved)
        path_name=name_matches(company,path)
        content_name=name_matches(company,titles+' '+context)
        city=' '+folded(company.city)+' '
        geo=bool(company.city) and (city in ' '+folded(path)+' ' or city in ' '+folded(titles)+' ')
        url_exact=extract_cnpjs(path)=={company.cnpj}
        content_exact=company.cnpj in saved_numbers
        # Páginas de município/categoria/listas nunca são específicas só porque
        # uma das muitas empresas mencionadas contém o CNPJ alvo.
        generic=bool(re.search(r'(?:^|/)(?:busca[^/]*|search|categorias?|lista-de-empresas|estado)(?:/|$)',path,re.I))
        specific=not generic and (url_exact or path_name or (parts.path=='/' and content_exact and content_name))
        if not specific or not (url_exact or (content_exact and content_name) or (path_name and geo)):
            continue
        confidence='ALTA' if content_exact and content_name else 'MEDIA'
        evidence=[];alerts=[]
        if url_exact:evidence.append('CNPJ exato na URL armazenada')
        if content_exact:evidence.append('CNPJ exato em evidência de página salva')
        if path_name:evidence.append('Nome empresarial correspondente no caminho da URL')
        if content_name:evidence.append('Nome empresarial correspondente na evidência salva')
        if geo:evidence.append('Município correspondente na URL ou título')
        for title in (f.get('title','') for f in saved):
            location=re.search(r'\bem\s+(.+?)\s*[-|]',title,re.I)
            if location and company.city and folded(location.group(1))!=folded(company.city):
                alerts.append('Localidade do título difere do município informado: '+location.group(1).strip())
        if alerts:confidence='BAIXA'
        kind=candidate_kind(url)
        source_type={'BUSINESS_DIRECTORY':'DIRETORIO_EMPRESARIAL','SOCIAL_NETWORK':'REDE_SOCIAL'}.get(kind,
                    'DOCUMENTO_INSTITUCIONAL_TERCEIRO' if re.search(r'(?:^|/)(?:act|cct)(?:/|$)',path,re.I) else
                    'PAGINA_DE_REFERENCIA')
        selected.append(dict(cnpj=company.cnpj,url=url,source_type=source_type,confidence=confidence,
            identity_match='CNPJ_EXATO' if url_exact or content_exact else 'NOME_E_MUNICIPIO',
            evidence=evidence,alerts=list(dict.fromkeys(alerts)),needs_review=not content_exact or bool(alerts),
            official_website=False,current_access_checked=False))
    return selected
