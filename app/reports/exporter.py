import csv
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone

from app.models import Validation
from app.services.site_outcome import official_website, public_validation, result_status
from app.services.site_rules import candidate_groups
from app.services.references import useful_references
from app.models import Company

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else ""


def safe_cell(value):
    text = str(value or "")
    # Proteção de fórmula em CSV e Excel; remover controles que o XML não aceita.
    text = "".join(c for c in text if ord(c) >= 32 or c in "\n\t\r")
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        text = "'" + text
    return text[:32767]


def export_rows(rows):
    output = []
    for row in rows:
        company = json.loads(row["company"] or "{}")
        saved = json.loads(row["validation"] or "{}")
        public = public_validation(Validation(**saved))
        val = public.to_dict()
        groups = candidate_groups(json.loads(row['candidates'] or '[]'))
        references=val.get('signals',{}).get('useful_references')
        if references is None:
            references=useful_references(Company(**dict(company,cnpj=row['cnpj'])),json.loads(row['candidates'] or '[]'),val.get('page_evidence',[]),val['website'])
        status = row['status']
        if status in {'CONCLUIDO','PARCIAL_DADOS','SEM_SITE_OFICIAL_CONFIRMADO',
                      'PENDENTE_EVIDENCIA_INSTITUCIONAL','PENDENTE_VALIDACAO_SITE_OFICIAL','FALHA_VALIDACAO_SITE_OFICIAL'}:
            status = result_status(public)
        output.append({
            "CNPJ": row["cnpj"], "CNPJ original": row["original"],
            "Razão social": company.get("legal_name", ""), "Nome fantasia": company.get("trade_name", ""),
            "Município": company.get("city", ""), "UF": company.get("state", ""),
            "Situação cadastral": company.get("registration_status", ""), "CNAE principal": company.get("main_cnae", ""),
            "Website": val.get("website", ""), "Domínio": val.get("domain", ""),
            "Fonte": row["source"], "Confiança": val['confidence'] if saved else row["confidence"], "Status": status,
            "Evidências": "; ".join(val.get("evidence", [])), "Pontuação": str(val.get("score", 0)),
            "Confirmado": "SIM" if val.get("confirmed") else "NAO", "Data": timestamp(row["updated_at"]),
            "Erro": row["error"], "Tentativas": str(row["attempts"]),
            "Erro histórico": val.get('signals',{}).get('historical_error',''),
            "Candidatos": "; ".join(json.loads(row["candidates"] or "[]")),
            "Redes sociais (não verificadas)": "; ".join(groups['social_networks']),
            "Diretórios e listagens (não oficiais)": "; ".join(groups['business_directories']),
            "Referências úteis (não são Website oficial)": json.dumps(references,ensure_ascii=False),
            "Origem dos campos empresariais": json.dumps(row.get("company_sources", {}), ensure_ascii=False, sort_keys=True),
            "Candidato pendente de validação": val.get("signals", {}).get("candidate_url", ""),
            "Candidato para revisão": val.get("signals", {}).get("review_candidate_url", ""),
            "Evidência documental": json.dumps(val.get("page_evidence", []), ensure_ascii=False),
            "Auditoria da descoberta": json.dumps(row.get("discovery_audit", {}), ensure_ascii=False),
        })
    return output


HEADERS = ["CNPJ", "CNPJ original", "Razão social", "Nome fantasia", "Município", "UF", "Situação cadastral",
           "CNAE principal", "Website", "Domínio", "Fonte", "Confiança", "Status", "Evidências", "Pontuação",
           "Confirmado", "Data", "Erro", "Erro histórico", "Tentativas", "Candidatos", "Redes sociais (não verificadas)", "Diretórios e listagens (não oficiais)", "Referências úteis (não são Website oficial)", "Origem dos campos empresariais", "Candidato pendente de validação", "Candidato para revisão", "Evidência documental", "Auditoria da descoberta"]


def write_csv(path, rows, headers):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, headers, delimiter=";", quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            writer.writerow({h: safe_cell(row.get(h, "")) for h in headers})
    os.replace(temporary, path)


def make_report(db, ids, imported, quota, started_at, mode, completed, interrupted=False):
    rows = db.rows(ids)
    total = len(rows)
    vals = [public_validation(Validation(**json.loads(r["validation"] or "{}"))).to_dict() for r in rows]
    statuses = [r['Status'] for r in export_rows(rows)]
    identified = sum(bool(json.loads(r["company"] or "{}").get("legal_name") or
                          json.loads(r["company"] or "{}").get("trade_name")) for r in rows)
    candidate_companies = sum(bool(json.loads(r["candidates"] or "[]")) or bool(v.get("website")) for r, v in zip(rows, vals))
    websites = sum(bool(official_website(v)) for v in vals)
    confirmed = websites
    review = sum(not official_website(v) and (v.get("confidence") in {"MEDIA", "BAIXA", "PENDENTE"} or bool(v.get('signals',{}).get('review_candidate_url'))) for v in vals)
    usage = db.usage(started_at)
    return dict(modo=mode, interrompido=interrupted, inicio_utc=timestamp(started_at),
                fim_utc=timestamp(time.time()), tempo_total_segundos=round(time.time() - started_at, 3),
                importacao=imported.counts, escopo_selecionado=total,
                processados_nesta_execucao=completed,
                processados_acumulados=sum(r["attempts"] > 0 and r["status"] != "PENDENTE" for r in rows),
                empresas_identificadas=identified, empresas_com_candidatos=candidate_companies,
                empresas_com_dados_da_planilha=sum("PLANILHA_LOCAL" in r["company_sources"].values() for r in rows),
                colunas_empresariais_importadas=imported.business_columns,
                websites_encontrados=websites,
                websites_acessiveis=sum(bool(official_website(v)) and bool(v.get("accessible")) for v in vals),
                websites_validados_alta_confianca=confirmed, sites_exigem_revisao=review,
                sem_website=total - websites,
                com_erros=sum(s in {"ERRO", "LIMITE_TEMPORARIO", "FALHA_VALIDACAO_SITE_OFICIAL"} for s in statuses),
                candidatos_pendentes=statuses.count('PENDENTE_VALIDACAO_SITE_OFICIAL'),
                falhas_validacao=statuses.count('FALHA_VALIDACAO_SITE_OFICIAL'),
                sem_site_oficial_confirmado=statuses.count('SEM_SITE_OFICIAL_CONFIRMADO'),
                status=dict(Counter(statuses)),
                requisicoes_nesta_execucao=usage, requisicoes_acumuladas=db.usage(),
                cotas=quota.summary(),
                custo_adicional_efetivo_usd=0 if not usage else None,
                custo_verificacao="Nenhuma requisição externa nesta execução" if not usage else
                "Sem conciliação financeira do provedor; controles locais não comprovam faturamento",
                orcamento_maximo_adicional_usd=0,
                taxa_cobertura=websites / total if total else 0,
                taxa_confirmacao=confirmed / websites if websites else 0,
                definicoes={"cobertura": "empresas com Website / CNPJs selecionados",
                            "empresas_identificadas": "nome empresarial disponível, inclusive fornecido pela planilha; não comprova confirmação externa",
                            "PLANILHA_LOCAL": "dados fornecidos no arquivo; nenhuma confirmação externa implícita",
                            "confirmacao": "ALTA confirmada / empresas com Website",
                            "validados": "ALTA confirmada com evidências de titularidade; categorias, terceiros e candidatos ficam fora de Website",
                            "candidatos": "links encontrados; não representam confirmação de vínculo"})


def export_all(out, db, ids, imported, report):
    out.mkdir(parents=True, exist_ok=True)
    rows = export_rows(db.rows(ids))
    missing = [r for r in rows if not r["Website"]]
    review = [r for r in rows if not r['Website'] and (r["Confiança"] in {"MEDIA", "BAIXA", "PENDENTE"} or r['Candidato para revisão'])]
    audit_headers = ["linha", "original", "cnpj", "classificacao"]
    for filename, data, headers in (
        ("resultado_cnpjs.csv", rows, HEADERS), ("sem_website.csv", missing, HEADERS),
        ("revisao_manual.csv", review, HEADERS), ("auditoria_importacao.csv", imported.audit, audit_headers),
    ):
        write_csv(out / filename, data, headers)
    book = Workbook()
    book.remove(book.active)
    for name, data, headers in (("Resultados", rows, HEADERS), ("Sem website", missing, HEADERS),
                                 ("Revisão manual", review, HEADERS), ("Auditoria", imported.audit, audit_headers)):
        sheet = book.create_sheet(name)
        sheet.append(headers)
        for row in data:
            sheet.append([safe_cell(row.get(h, "")) for h in headers])
        sheet.freeze_panes = "C2" if name != "Auditoria" else "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="16324F")
        for column in sheet.columns:
            sheet.column_dimensions[column[0].column_letter].width = min(55, max(18, len(str(column[0].value)) + 3))
    temp = out / "resultado_cnpjs.tmp.xlsx"
    book.save(temp)
    book.close()
    os.replace(temp, out / "resultado_cnpjs.xlsx")
    temporary = out / "relatorio_execucao.json.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, out / "relatorio_execucao.json")
