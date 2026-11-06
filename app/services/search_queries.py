"""Consultas nominais; exclusões afetam descoberta, nunca substituem validação."""
from app.services.site_rules import brand_tokens, folded

# Fontes mais frequentes entre os candidatos reais desta POC. Não excluir a
# palavra CNPJ: o site oficial pode publicá-la nos seus próprios termos.
SEARCH_EXCLUSIONS = ('cnpja.com', 'datastone.com.br', 'opencnpj.com', 'cnpjcheck.com.br',
                     'infoville.com.br', 'serasaexperian.com.br', 'toreva.com.br', 'breela.com.br')


def nominal_name(name):
    tokens = name.replace('"', ' ').split()
    suffixes = {'ltda', 'limitada', 'eireli', 'me', 'epp', 'sa', 's/a', 's.a'}
    while tokens and tokens[-1].strip('.,').lower() in suffixes:
        tokens.pop()
    return ' '.join(tokens)


def queries_for(company):
    name = nominal_name(company.trade_name or company.legal_name)
    exclusions = ' '.join('-site:' + host for host in SEARCH_EXCLUSIONS)
    city = ' '.join(company.city.replace('"', ' ').split())
    location = ' '.join(v for v in (city, company.state) if v)
    if not name:
        return [f'"{company.cnpj}" site oficial {exclusions}']
    first = ' '.join(f'"{name}" {location} site oficial {exclusions}'.split())
    # Alternativa menos restritiva por marca distintiva + município, sem o
    # número cadastral que dominava os resultados anteriores.
    distinctive = brand_tokens(company)
    words = [t for t in name.split() if folded(t) in distinctive]
    brand = ' '.join(words) or name
    if company.trade_name and company.legal_name and folded(company.trade_name) != folded(company.legal_name):
        brand = nominal_name(company.legal_name)
    alternative = ' '.join(f'{brand} "{city}" {company.state} contato institucional {exclusions}'.split()) if city else (
        ' '.join(f'{brand} {company.state} contato institucional {exclusions}'.split()))
    return list(dict.fromkeys([first, alternative]))[:2]
