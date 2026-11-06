"""Consolidação local com fontes explícitas; não escolhe cópias por data de arquivo."""
import argparse
import csv
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from app.models import Company,Validation
from app.services.references import useful_references
from app.services.site_outcome import public_validation,result_status
from app.services.site_rules import RULES_VERSION,candidate_groups


def readonly(path):
    return closing(sqlite3.connect(f'file:{Path(path).resolve().as_posix()}?mode=ro',uri=True))


def consolidate(manifest,ledger,output):
    output=Path(output).resolve()
    if output.exists() and any(output.iterdir()):raise ValueError('Use uma saída nova e vazia')
    expected=manifest['expected_ids']
    if len(expected)!=20 or len(set(expected))!=20:raise ValueError('A consolidação requer exatamente vinte CNPJs distintos')
    sources={}
    for entry in manifest['sources']:
        with readonly(entry['database']) as db:
            db.row_factory=sqlite3.Row
            for cnpj in entry['ids']:
                if cnpj in sources:raise ValueError('CNPJ atribuído a duas fontes: '+cnpj)
                record=db.execute('SELECT * FROM companies WHERE cnpj=?',(cnpj,)).fetchone()
                if record is None:raise ValueError('CNPJ não encontrado na fonte: '+cnpj)
                row=dict(record)
                audit=db.execute('SELECT value FROM state WHERE key=?',('discovery_audit:'+cnpj,)).fetchone()
                sources[cnpj]=dict(record=row,audit=json.loads(audit[0]) if audit else {},database=str(Path(entry['database']).resolve()),label=entry['label'])
    if set(sources)!=set(expected):raise ValueError('Cobertura das fontes diferente dos vinte CNPJs esperados')
    with readonly(ledger) as db:
        usage=dict(db.execute('SELECT provider,count(*) FROM requests GROUP BY provider'))
        cached=[json.loads(v) for v, in db.execute("SELECT value FROM cache WHERE provider='validation'")]
    all_facts=[fact for v in cached for fact in v.get('page_evidence',[])]
    manual={r['cnpj']:r for r in manifest.get('manual_checks',[])}
    if set(manual)-set(expected):raise ValueError('Verificação manual fora do escopo')
    records=[];references=[]
    for cnpj in expected:
        source=sources[cnpj];row=source['record']
        company=Company(**json.loads(row['company']))
        validation=public_validation(Validation(**json.loads(row['validation'] or '{}')))
        status=result_status(validation)
        if validation.website:category='CONFIRMACAO_AUTOMATICA'
        elif cnpj in manual:category='VERIFICACAO_MANUAL_INFORMADA'
        elif status in {'PENDENTE_VALIDACAO_SITE_OFICIAL','FALHA_VALIDACAO_SITE_OFICIAL'}:category='CANDIDATO_PENDENTE'
        else:category='SEM_SITE_OFICIAL_CONFIRMADO'
        candidates=json.loads(row['candidates'] or '[]')
        refs=useful_references(company,candidates,all_facts+validation.page_evidence,validation.website)
        references.extend(refs)
        groups=candidate_groups(candidates)
        check=manual.get(cnpj,{})
        records.append(dict(cnpj=cnpj,company=company.legal_name,city=company.city,category=category,
            automatic_status=status,official_website=validation.website,automatic_confidence=validation.confidence,
            manual_website=check.get('url',''),manual_evidence=check.get('evidence',''),
            manual_origin=check.get('origin',''),pending_candidate=validation.signals.get('candidate_url','') if not validation.website else '',
            reference_count=len(refs),social_candidates=groups['social_networks'],source_label=source['label'],
            source_updated_at=row['updated_at'],historical_error=validation.signals.get('historical_error') or row['error'],
            evidence=validation.page_evidence))
    summary=dict(total=20,categories=dict(Counter(r['category'] for r in records)),references=len(references),
        companies_with_references=sum(r['reference_count']>0 for r in records),reference_confidence=dict(Counter(r['confidence'] for r in references)),
        usage_preserved=usage,external_queries=0,rules_version=RULES_VERSION,
        manual_definition='Relato explícito do usuário; não revalidado nesta consolidação e não promovido a ALTA automática')
    output.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(output/'consolidacao20.sqlite')) as db:
        db.executescript('CREATE TABLE consolidated_companies(cnpj TEXT PRIMARY KEY,record TEXT NOT NULL);'
            'CREATE TABLE source_records(cnpj TEXT PRIMARY KEY,record TEXT NOT NULL);'
            'CREATE TABLE useful_references(cnpj TEXT,url TEXT,record TEXT NOT NULL,PRIMARY KEY(cnpj,url));'
            'CREATE TABLE manual_checks(cnpj TEXT PRIMARY KEY,record TEXT NOT NULL);')
        db.executemany('INSERT INTO consolidated_companies VALUES (?,?)',[(r['cnpj'],json.dumps(r,ensure_ascii=False)) for r in records])
        db.executemany('INSERT INTO source_records VALUES (?,?)',[(c,json.dumps(sources[c],ensure_ascii=False)) for c in expected])
        db.executemany('INSERT INTO useful_references VALUES (?,?,?)',[(r['cnpj'],r['url'],json.dumps(r,ensure_ascii=False)) for r in references])
        db.executemany('INSERT INTO manual_checks VALUES (?,?)',[(c,json.dumps(r,ensure_ascii=False)) for c,r in manual.items()])
        db.commit()
    for name,value in [('consolidacao20.json',dict(summary=summary,companies=records,references=references)),('fontes.json',manifest)]:
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    with (output/'consolidacao20.csv').open('w',encoding='utf-8-sig',newline='') as f:
        fields=['cnpj','company','city','category','automatic_status','official_website','automatic_confidence','manual_website',
                'manual_evidence','pending_candidate','reference_count','source_label']
        writer=csv.DictWriter(f,fields,delimiter=';',extrasaction='ignore');writer.writeheader();writer.writerows(records)
    with (output/'referencias_uteis.csv').open('w',encoding='utf-8-sig',newline='') as f:
        fields=['cnpj','url','source_type','confidence','identity_match','evidence','alerts','needs_review']
        writer=csv.DictWriter(f,fields,delimiter=';',extrasaction='ignore');writer.writeheader();writer.writerows(references)
    return summary


def main(argv=None):
    cli=argparse.ArgumentParser(description='Consolidar vinte CNPJs offline, com referências e proveniência')
    cli.add_argument('--manifest',type=Path,required=True);cli.add_argument('--ledger',type=Path,required=True)
    cli.add_argument('--output',type=Path,required=True);args=cli.parse_args(argv)
    try:
        print(json.dumps(consolidate(json.loads(args.manifest.read_text(encoding='utf-8')),args.ledger,args.output),ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError,sqlite3.Error) as error:print(str(error));return 2


if __name__=='__main__':raise SystemExit(main())
