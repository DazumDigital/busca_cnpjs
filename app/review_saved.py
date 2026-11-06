"""Reavalia evidências derivadas já salvas. Nenhum cliente HTTP é utilizado."""
import argparse
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from app.models import Company, Validation
from app.reports.exporter import export_all, write_csv
from app.services.importer import ImportResult
from app.services.site_rules import RULES_VERSION, brand_tokens, candidate_groups, candidate_kind, content_directory_reason, domain_brand_match, extract_cnpjs, rank_candidates, tenant_brand_match, url_reason
from app.services.validator import fold, name_tokens, owned_institutional_fact, safe_url, same_site, tenant_identity_evidence
from app.services.site_outcome import public_validation, result_status
from app.services.references import useful_references
from app.storage.database import Database


def from_saved(company, saved):
    url=saved.get('website','')
    if not url: return None
    try:
        clean,host,_=safe_url(url)
    except ValueError:
        return Validation(evidence=['URL_INSEGURA'],rules_version=RULES_VERSION,site_type='THIRD_PARTY')
    reason=url_reason(clean,company.cnpj)
    facts=saved.get('page_evidence',[])
    for fact in facts:
        reason=reason or content_directory_reason(fact.get('title',''),fact.get('owner_context',''))
    if reason:
        return Validation(evidence=[reason],rules_version=RULES_VERSION,site_type='THIRD_PARTY',page_evidence=facts)
    if not facts: return None
    facts=[f for f in facts if f.get('source')=='PUBLIC_PAGE_DERIVED_EVIDENCE' and
           len(f.get('content_sha256',''))==64 and same_site(f.get('url',''),clean)]
    if not facts: return None
    numbers=set().union(*(set(f.get('cnpjs',[]))|extract_cnpjs(f.get('owner_context','')) for f in facts))
    if numbers and company.cnpj not in numbers:
        return Validation(evidence=['CNPJ_DIVERGENTE_EVIDENCIA_SALVA'],rules_version=RULES_VERSION,
                          site_type='CNPJ_DIVERGENTE',page_evidence=facts)
    heading=' '+fold(facts[0].get('title',''))+' '
    text=' '+fold(' '.join(f.get('title','')+' '+f.get('owner_context','') for f in facts))+' '
    name_match=any(tokens and all(' '+t+' ' in text for t in tokens) for tokens in
                   (name_tokens(company.legal_name),name_tokens(company.trade_name)))
    brand=domain_brand_match(company,host)
    heading_brand=any(' '+t+' ' in heading for t in brand_tokens(company))
    owner=any(owned_institutional_fact(company,f) for f in facts)
    tenant_paths=tenant_identity_evidence(company,host,facts)
    tenant_identity=bool(tenant_paths) and numbers=={company.cnpj}
    exact=company.cnpj in numbers
    strong=exact and owner and name_match and (brand or tenant_identity) and heading_brand
    if not strong and not ((brand or tenant_brand_match(company,host)) and heading_brand): return None
    return Validation(clean,host.removeprefix('www.'),'ALTA' if strong else 'MEDIA',100 if strong else 50,
                      ['Evidência institucional derivada já armazenada; sem consulta nova',
                       'Confirmação baseada em CNPJ/contexto, nunca em referência manual' if strong else 'REVISÃO MANUAL: evidência parcial'],
                      False,strong,RULES_VERSION,'CORPORATE' if strong else 'CORPORATE_CANDIDATE',
                      dict(exact_cnpj=exact,legal_owner=owner,name_match=name_match,brand_domain=brand,heading_brand=heading_brand,
                           tenant_identity_confirmed=tenant_identity,tenant_evidence_paths=tenant_paths),
                      [f['url'] for f in facts],facts)


def review(source,ledger,output,ids):
    source,ledger,output=Path(source).resolve(),Path(ledger).resolve(),Path(output).resolve()
    if not ids or len(set(ids))!=len(ids): raise ValueError('Informe CNPJs distintos já armazenados')
    with closing(sqlite3.connect(f'file:{source.as_posix()}?mode=ro',uri=True)) as conn:
        if any(not conn.execute('SELECT 1 FROM companies WHERE cnpj=?',(c,)).fetchone() for c in ids):
            raise ValueError('Todos os CNPJs devem existir na base de origem')
    if output.exists() and any(output.iterdir()): raise ValueError('Use uma saída nova e vazia')
    output.mkdir(parents=True,exist_ok=True)
    target=output/'revisao.sqlite'
    with closing(sqlite3.connect(f'file:{source.as_posix()}?mode=ro',uri=True)) as conn:
        with closing(sqlite3.connect(target)) as copy: conn.backup(copy)
    with closing(sqlite3.connect(f'file:{ledger.as_posix()}?mode=ro',uri=True)) as conn:
        cached=[json.loads(r[0]) for r in conn.execute("SELECT value FROM cache WHERE provider='validation'")]
        usage=dict(conn.execute('SELECT provider,count(*) FROM requests GROUP BY provider'))
    db=Database(target);changes=[]
    try:
        db.conn.execute('CREATE TABLE IF NOT EXISTS evidence_review_history(cnpj TEXT, previous_record TEXT,reviewed_at REAL)')
        for cnpj in ids:
            row=dict(db.row(cnpj)); company=Company(**json.loads(row['company']))
            previous=json.loads(row['validation'] or '{}')
            previous_audit=db.get_state('discovery_audit:'+cnpj,{})
            candidates=list(dict.fromkeys(json.loads(row['candidates'] or '[]')+([previous['website']] if previous.get('website') else [])))
            eligible=[]; rejected=[];best=None;facts=[];audit={};failed=[]
            for url in candidates:
                try:
                    clean,_,_=safe_url(url);reason=url_reason(clean,cnpj)
                except ValueError:
                    rejected.append(dict(url=url,reason='URL_INSEGURA'))
                    audit[url]=dict(decision='REJECTED',reason='URL_INSEGURA',rules_version=RULES_VERSION)
                    continue
                old=previous_audit.get(url,{})
                evidence=[v for v in cached+[previous] if v.get('website')==clean]
                facts.extend(f for v in evidence for f in v.get('page_evidence',[]) if f not in facts)
                results=[from_saved(company,v) for v in evidence]
                bad=next((v for v in results if v and v.site_type in {'THIRD_PARTY','CNPJ_DIVERGENTE'}),None)
                if not reason and (old.get('decision')=='REJECTED' or old.get('site_type') in {'THIRD_PARTY','CNPJ_DIVERGENTE'}):
                    reason=old.get('reason') or 'REJEICAO_DOCUMENTADA_ANTERIORMENTE: '+old['site_type']
                if reason or bad:
                    reason=reason or ';'.join(bad.evidence)
                    rejected.append(dict(url=url,reason=reason))
                    audit[url]=dict(decision='REJECTED',reason=reason,rules_version=RULES_VERSION)
                    continue
                eligible.append(clean)
                audit[url]=dict(decision='PENDING_EVIDENCE',reason='Vínculo não confirmado pelas evidências locais',rules_version=RULES_VERSION)
                if old.get('decision')=='ERROR':
                    failed.append(clean)
                    audit[url]=dict(old,rules_version=RULES_VERSION)
                for result in results:
                    if result and (best is None or (result.confirmed,result.score)>(best.confirmed,best.score)):best=result
            eligible=rank_candidates(company,eligible)
            if best:
                priority=best.website
                best=public_validation(best)
                audit[priority]=dict(decision='CONFIRMED' if best.confirmed else 'PENDING_EVIDENCE',
                                     confidence=best.confidence,rules_version=RULES_VERSION)
            elif eligible:
                priority=failed[0] if failed else eligible[0]
                best=Validation(confidence='PENDENTE',rules_version=RULES_VERSION,site_type='VALIDATION_ERROR' if failed else 'CORPORATE_PENDING',
                                signals={'candidate_url':priority},evidence=['Sem conteúdo institucional suficiente salvo; referência manual não gera ALTA',
                                'Erro anterior: '+row['error']],page_evidence=facts)
            else:
                priority=''
                best=Validation(rules_version=RULES_VERSION,site_type='THIRD_PARTY_ONLY',page_evidence=facts,
                                evidence=['Nenhum site oficial confirmado pelos candidatos armazenados; isso não comprova ausência de site',
                                          'Apenas terceiros ou identidade divergente nas evidências salvas'])
            previous_signals=previous.get('signals',{})
            review_url=(previous.get('website') or previous_signals.get('review_candidate_url') or
                        previous_signals.get('candidate_url') or (rejected[0]['url'] if rejected else priority))
            if review_url:best.signals['review_candidate_url']=review_url
            best.signals['historical_error']=row['error']
            groups=candidate_groups(candidates)
            best.signals.update(groups)
            best.signals['useful_references']=useful_references(company,candidates,facts,best.website)
            for url, entry in audit.items():
                entry['source_kind']=candidate_kind(url)
                entry['decision_scope']='WEBSITE_OFICIAL'
            status=result_status(best)
            archived=dict(row,discovery_audit=previous_audit)
            db.conn.execute('INSERT INTO evidence_review_history VALUES (?,?,?)',(cnpj,json.dumps(archived,ensure_ascii=False),time.time()))
            db.set_state('discovery_audit:'+cnpj,audit)
            current_error=';'.join(dict.fromkeys(audit.get(u,{}).get('reason','VALIDACAO_FALHOU') for u in failed)) if status=='FALHA_VALIDACAO_SITE_OFICIAL' else ''
            db.update(cnpj,validation=best.to_dict(),confidence=best.confidence,status=status,error=current_error)
            changes.append(dict(CNPJ=cnpj,Empresa=company.legal_name,Status=status,Confianca=best.confidence,
                                Website=best.website,Candidato=priority,Candidato_revisao=review_url,
                                Redes_sociais_nao_verificadas=groups['social_networks'],Diretorios_descartados=groups['business_directories'],
                                Referencias_uteis=best.signals['useful_references'],
                                Erro_historico=row['error'],Rejeicoes=rejected))
        imported=ImportResult([(c,db.row(c)['original']) for c in ids],[],dict(selecionados=len(ids)))
        report=dict(modo='REVISAO_OFFLINE_EVIDENCIAS',consultas_novas=0,consumo_preservado=usage,resultados=changes)
        export_all(output,db,ids,imported,report)
        write_csv(output/'comparativo.csv',changes,list(changes[0]))
        return report
    finally: db.close()


def main(argv=None):
    cli=argparse.ArgumentParser(description='Revisar evidências locais sem Serper ou websites')
    cli.add_argument('--database',type=Path,required=True);cli.add_argument('--ledger',type=Path,required=True)
    cli.add_argument('--output',type=Path,required=True);cli.add_argument('--cnpjs',nargs='+',required=True)
    args=cli.parse_args(argv)
    try:
        print(json.dumps(review(args.database,args.ledger,args.output,args.cnpjs),ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError,sqlite3.Error) as error:
        print(str(error));return 2


if __name__=='__main__':raise SystemExit(main())
