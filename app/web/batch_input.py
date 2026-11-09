"""Mesma normalização de CNPJ para entrada individual e lotes pequenos."""
import re
from app.services.importer import valid_cnpj
from app.web.store import WebError


def analyze_input(value):
    if not isinstance(value, str) or len(value) > 3500:
        raise WebError('REQUISICAO_INVALIDA', 422)
    entries = [s.strip() for s in re.split(r'[\n,;]+', value) if s.strip()]
    valid, invalid, duplicate = [], [], []
    for entry in entries:
        cnpj = re.sub(r'[^0-9]', '', entry)
        if not re.fullmatch(r'[0-9./\-\s]+', entry) or not valid_cnpj(cnpj):
            invalid.append(entry[:100])
        elif cnpj in valid:
            duplicate.append(cnpj)
        else:
            valid.append(cnpj)
    return dict(identificados=len(entries), validos=len(valid), invalidos=len(invalid),
                duplicados=len(duplicate), cnpjs=valid, entradas_invalidas=invalid)


def validated_input(value):
    result = analyze_input(value)
    if result['validos'] > 15:
        raise WebError('LIMITE_LOTE', 422)
    if not result['validos']:
        raise WebError('CNPJ_INVALIDO', 422)
    return result
