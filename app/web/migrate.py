"""Importação explícita e idempotente, com a origem sempre somente leitura."""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from sqlalchemy import insert

from app.services.importer import valid_cnpj
from app.web.results import public_result
from app.web.store import organizations, companies, imports, import_records


def import_consolidation(store, source, organization='dazumdigital'):
    path = Path(source).resolve()
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True) as db:
        records = [json.loads(r) for r, in db.execute('SELECT record FROM consolidated_companies')]
        originals = {c: json.loads(r)['record'] for c, r in db.execute('SELECT cnpj,record FROM source_records')}
        refs = [json.loads(r) for r, in db.execute('SELECT record FROM useful_references')]
        manual = {c: json.loads(r) for c, r in db.execute('SELECT cnpj,record FROM manual_checks')}
    if len(records) != 20 or len({r['cnpj'] for r in records}) != 20:
        raise ValueError('A origem deve conter os vinte CNPJs consolidados')
    payloads = []
    for record in records:
        cnpj = record['cnpj']
        if not valid_cnpj(cnpj):
            raise ValueError('CNPJ inválido na origem')
        original = originals[cnpj]
        identity = json.loads(original['company'])
        if identity.get('cnpj') != cnpj or original.get('cnpj') != cnpj:
            raise ValueError('Identidades divergentes na origem')
        result = public_result(record, identity, json.loads(original['validation'] or '{}'),
                               [r for r in refs if r['cnpj'] == cnpj], manual.get(cnpj))
        payloads.append((cnpj, json.dumps(result, ensure_ascii=False)))
    with store.transaction() as conn:
        store.lock_org(conn, organization)
        imported = store.upsert(conn, imports, dict(organization_id=organization, source_hash=source_hash, record_count=20, created_at=time.time()),
                                ['organization_id', 'source_hash'])
        if imported is not None:
            for record in records:
                cnpj = record['cnpj']
                conn.execute(insert(import_records).values(organization_id=organization, source_hash=source_hash, cnpj=cnpj,
                    record=json.dumps(dict(classification=record, original=originals[cnpj], manual=manual.get(cnpj)), ensure_ascii=False)))
            for cnpj, packed in payloads:
                # Existing newer web results are not overwritten by an old snapshot.
                store.upsert(conn, companies, dict(organization_id=organization, cnpj=cnpj, payload=packed, source_hash=source_hash),
                             ['organization_id', 'cnpj'])
    return dict(registros=20, sha256=source_hash, importacao_nova=imported is not None)
