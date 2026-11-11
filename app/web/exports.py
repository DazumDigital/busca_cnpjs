"""Exportação somente do contrato público; nenhuma nova consulta ou cobrança."""
import csv
import io
import json
from datetime import datetime, timezone
from openpyxl import Workbook

COLUMNS = ['CNPJ', 'Razão social', 'Nome fantasia', 'Município', 'UF', 'Situação cadastral',
    'Website oficial confirmado', 'Verificação manual informada', 'Websites candidatos',
    'Redes sociais', 'Links de referência', 'Fontes, confiança e evidências',
    'Status da validação', 'Status do processamento', 'Erro', 'Data da consulta', 'Data da pesquisa de origem',
    'Site da matriz (filial)', 'Indicado pelo painel do Google']


def text(value):
    return str(value or '')


def rows(items):
    output, links = [], []
    for item in items:
        result = item.get('resultado') or {}
        company = result.get('empresa') or {}
        official = result.get('website_oficial') or {}
        manual = result.get('verificacao_manual') or {}
        candidates = result.get('possiveis_sites') or []
        matriz = result.get('site_matriz') or []
        panel = result.get('indicado_google') or []
        socials = result.get('redes_sociais') or []
        refs = result.get('referencias') or []
        evidence = []
        for category, values in [('OFICIAL', [official]), ('MANUAL', [manual]), ('SITE_DA_MATRIZ', matriz),
                                 ('CANDIDATO', candidates), ('PAINEL_GOOGLE', panel),
                                 ('REDE_SOCIAL', socials), ('REFERENCIA', refs)]:
            for link in values:
                if not link or not link.get('url'):
                    continue
                details = dict(categoria=category, url=link['url'], tipo=link.get('tipo'),
                    confianca=link.get('confianca'), evidencia=link.get('evidencia'),
                    indicado_google=link.get('indicado_google', False),
                    ressalvas=link.get('ressalvas', []), paginas=link.get('paginas', []))
                evidence.append(details)
                links.append([item['cnpj'], category, link['url'], text(link.get('tipo')),
                    text(link.get('confianca')), json.dumps(details, ensure_ascii=False)])
        output.append([item['cnpj'], text(company.get('razao_social')), text(company.get('nome_fantasia')),
            text(company.get('municipio')), text(company.get('uf')), text(company.get('situacao_cadastral')),
            text(official.get('url')), text(manual.get('url')), '\n'.join(x['url'] for x in candidates),
            '\n'.join(x['url'] for x in socials), '\n'.join(x['url'] for x in refs),
            json.dumps(evidence, ensure_ascii=False), text(result.get('status')), item['status'],
            text(item.get('erro')), iso(item.get('criado_em')), iso(company.get('pesquisado_em')),
            # Colunas novas no fim, para não deslocar planilhas já montadas.
            '\n'.join(x['url'] for x in matriz), '\n'.join(x['url'] for x in panel)])
    return output, links


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if isinstance(value, (int, float)) else text(value)


def csv_cell(value, cnpj=False):
    # Excel auto-converts leading-zero identifiers and can execute formula cells.
    value = text(value)
    if value and (cnpj or value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n'))):
        return "'" + value
    return value


XLSX_MAX = 32000  # o Excel aceita até 32.767 caracteres por célula


def xlsx_cell(value):
    # Trechos de páginas podem trazer caracteres de controle que o formato XLSX
    # proíbe (openpyxl recusaria a planilha inteira) ou textos longos demais.
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    value = ILLEGAL_CHARACTERS_RE.sub('', text(value))
    return value if len(value) <= XLSX_MAX else value[:XLSX_MAX - 1] + '…'


def export(items, format):
    values, links = rows(items)
    if format == 'csv':
        buffer = io.StringIO(newline='')
        writer = csv.writer(buffer, delimiter=';', quoting=csv.QUOTE_ALL)
        writer.writerow(COLUMNS)
        writer.writerows([[csv_cell(v, i == 0) for i, v in enumerate(row)] for row in values])
        return buffer.getvalue().encode('utf-8-sig'), 'text/csv; charset=utf-8', 'csv'
    if format != 'xlsx':
        raise ValueError('Formato inválido')
    book = Workbook()
    sheet = book.active
    sheet.title = 'Empresas'
    sheet.append(COLUMNS)
    for row in values:
        sheet.append([xlsx_cell(v) for v in row])
    detail = book.create_sheet('Links')
    detail.append(['CNPJ', 'Categoria', 'URL', 'Tipo de fonte', 'Confiança', 'Evidências e ressalvas'])
    for row in links:
        detail.append([xlsx_cell(v) for v in row])
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    for table in book:
        table.freeze_panes = 'A2'
        table.auto_filter.ref = table.dimensions
        for row in table:
            for cell in row:
                # Literal strings prevent formula injection; identifiers keep zeros.
                cell.data_type = 's'
                cell.number_format = '@'
                cell.alignment = Alignment(vertical='top', wrap_text=True)
                if cell.row == 1:
                    cell.font = Font(bold=True, color='FFFFFF')
                    cell.fill = PatternFill('solid', fgColor='0A1D3F')
        for column in range(1, table.max_column + 1):
            table.column_dimensions[get_column_letter(column)].width = 20 if column == 1 else 38
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue(), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'xlsx'
