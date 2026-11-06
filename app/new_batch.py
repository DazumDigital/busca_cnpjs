"""Seleciona CNPJs inéditos para um lote independente, sem executar buscas."""
import argparse
import csv
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from app.config import Settings,load_policies
from app.pilot3 import budget_status,small_settings
from app.reclassify import reclassify
from app.services.importer import ImportResult,import_file
from app.services.search_queries import queries_for
from app.models import Company
from app.storage.database import Database


def researched_ids(data_root):
    ids=set()
    for path in Path(data_root).rglob('*.sqlite'):
        with closing(sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro',uri=True)) as conn:
            tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'companies' not in tables:continue
            ids.update(r[0] for r in conn.execute("""SELECT cnpj FROM companies WHERE attempts>0 OR
                            COALESCE(candidates,'[]') NOT IN ('[]','null','')"""))
    return ids


def prepare_new(input_file,source,data_root,output,policies,settings,limit=10):
    if not 1<=limit<=10:raise ValueError('O próximo lote permite no máximo dez CNPJs')
    settings=small_settings(settings)
    imported=import_file(Path(input_file),1000)
    used=researched_ids(data_root)
    fresh=[(c,original) for c,original in imported.selected if c not in used]
    if not fresh:raise ValueError('Nenhum CNPJ inédito: a entrada contém somente empresas já pesquisadas ou inválidas')
    budget=budget_status(source,policies,settings)
    # O executor pequeno existente limita a três. A cota não é ampliada para
    # fabricar espaço para dez; duas pesquisas por empresa são reservadas.
    count=min(limit,3,budget['serper']['restantes_locais']//2,len(fresh))
    if count<1:raise ValueError('Cota local insuficiente para um novo lote; não reiniciar contadores')
    selected=fresh[:count];ids=[c for c,_ in selected]
    output=Path(output).resolve();source=Path(source).resolve()
    reclassify(source,output,10)
    counts=dict(imported.counts,selecionados=count)
    audit=[]
    for row in imported.audit:
        row=dict(row)
        if row['classificacao']=='SELECIONADO' and row['cnpj'] not in ids:
            row['classificacao']='JA_PESQUISADO' if row['cnpj'] in used else 'FORA_LIMITE_LOTE_OU_ORCAMENTO'
        audit.append(row)
    subset=ImportResult(selected,audit,counts,{c:imported.companies[c] for c in ids if c in imported.companies},imported.business_columns)
    db=Database(output/'reclassificacao.sqlite')
    try:
        db.ingest(subset)
        companies=[Company(**json.loads(db.row(c)['company'] or json.dumps({'cnpj':c}))) for c in ids]
    finally:db.close()
    prepared=output/'entrada_novo_lote.csv'
    with prepared.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream,delimiter=';');writer.writerow(['CNPJ','razao_social','nome_fantasia','municipio','UF','situacao_cadastral'])
        writer.writerows([c.cnpj,c.legal_name,c.trade_name,c.city,c.state,c.registration_status] for c in companies)
    plan=dict(mode='PREPARACAO_OFFLINE_NAO_EXECUTADA',new_unsearched=True,source_database=str(source),
              working_database=str(output/'reclassificacao.sqlite'),input=str(prepared),output=str(output/'execucao'),ids=ids,
              limits=dict(companies=count,concurrency=1,queries_per_company=2,retries=0,serper_new_attempts=2*count,website_new_attempts=40),
              initial_usage={k:v['usadas'] for k,v in budget.items()},budget=budget,
              candidate_analysis=[dict(cnpj=c,total_candidates=0,corporate_candidates=[]) for c in ids],
              queries=[dict(cnpj=c.cnpj,company=c.legal_name,queries=queries_for(c)) for c in companies],
              selection=dict(requested_max=limit,effective=count,reason='limite conservador/cota local preservada',already_researched=len(used)))
    (output/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
    return plan


def main(argv=None):
    cli=argparse.ArgumentParser(description='Preparar lote independente de CNPJs ainda não pesquisados, sem buscas')
    cli.add_argument('--input',type=Path,required=True);cli.add_argument('--source-database',type=Path,default=Path('data/poc.sqlite'))
    cli.add_argument('--data-root',type=Path,default=Path('data'));cli.add_argument('--output',type=Path,required=True)
    cli.add_argument('--limit',type=int,default=10);cli.add_argument('--env-file',type=Path,default=Path('.env'))
    cli.add_argument('--policies',type=Path,default=Path('provider_verification.json'))
    args=cli.parse_args(argv)
    try:
        plan=prepare_new(args.input,args.source_database,args.data_root,args.output,load_policies(args.policies),Settings.from_env(args.env_file),args.limit)
        print(json.dumps(dict(plano=str(args.output/'plan.json'),ids=plan['ids'],selection=plan['selection'],consultas_novas=0),ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError,sqlite3.Error) as error:print(str(error));return 2


if __name__=='__main__':raise SystemExit(main())
