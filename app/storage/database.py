import json
import sqlite3
import time
from dataclasses import asdict
from contextlib import contextmanager
from pathlib import Path

from app.models import Company


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS companies (
              cnpj TEXT PRIMARY KEY, original TEXT NOT NULL, position INTEGER NOT NULL,
              company TEXT, validation TEXT, candidates TEXT, source TEXT DEFAULT '',
              status TEXT NOT NULL DEFAULT 'PENDENTE', confidence TEXT DEFAULT 'PENDENTE',
              attempts INTEGER DEFAULT 0, error TEXT DEFAULT '', updated_at REAL);
            CREATE INDEX IF NOT EXISTS idx_status ON companies(status, position);
            CREATE TABLE IF NOT EXISTS imports (
              id INTEGER PRIMARY KEY, created_at REAL, counts TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
              import_id INTEGER, line INTEGER, original TEXT, cnpj TEXT, kind TEXT,
              FOREIGN KEY(import_id) REFERENCES imports(id));
            CREATE INDEX IF NOT EXISTS idx_audit_import ON audit(import_id);
            CREATE TABLE IF NOT EXISTS company_field_sources (
              cnpj TEXT NOT NULL, field TEXT NOT NULL, source TEXT NOT NULL,
              import_id INTEGER, updated_at REAL NOT NULL,
              PRIMARY KEY(cnpj, field),
              FOREIGN KEY(cnpj) REFERENCES companies(cnpj),
              FOREIGN KEY(import_id) REFERENCES imports(id));
            CREATE TABLE IF NOT EXISTS requests (
              id INTEGER PRIMARY KEY, provider TEXT, started_at REAL, outcome TEXT,
              http_status INTEGER);
            CREATE INDEX IF NOT EXISTS idx_usage ON requests(provider, started_at);
            CREATE TABLE IF NOT EXISTS cache (
              provider TEXT, key TEXT, value TEXT, expires_at REAL,
              PRIMARY KEY(provider, key));
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY, started_at REAL, finished_at REAL, mode TEXT,
              stage INTEGER, report TEXT);
        """)

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def close(self):
        self.conn.close()

    def ingest(self, imported):
        with self.transaction():
            existing = {r[0] for r in self.conn.execute("SELECT cnpj FROM companies")}
            if len(existing | {v[0] for v in imported.selected}) > 1000:
                raise ValueError("Este banco já alcançaria mais de 1000 CNPJs únicos; POC bloqueada")
            cursor = self.conn.execute("INSERT INTO imports(created_at,counts) VALUES (?,?)",
                                       (time.time(), json.dumps(imported.counts)))
            import_id = cursor.lastrowid
            self.conn.executemany("INSERT INTO audit VALUES (?,?,?,?,?)", [
                (import_id, a["linha"], a["original"], a["cnpj"], a["classificacao"])
                for a in imported.audit])
            pos = self.conn.execute("SELECT COALESCE(MAX(position),0) FROM companies").fetchone()[0]
            for cnpj, original in imported.selected:
                pos += 1
                self.conn.execute("INSERT OR IGNORE INTO companies(cnpj,original,position) VALUES (?,?,?)",
                                  (cnpj, original, pos))
                if cnpj in imported.companies:
                    self.save_company(cnpj, imported.companies[cnpj], "PLANILHA_LOCAL", import_id=import_id, overwrite=True)
        return import_id

    def company_sources(self, cnpj):
        return {r["field"]: r["source"] for r in self.conn.execute(
            "SELECT field,source FROM company_field_sources WHERE cnpj=?", (cnpj,))}

    def save_company(self, cnpj, incoming, origin, *, import_id=None, overwrite=False):
        """Merge sem apagar campos com vazios; proveniência separada do vínculo com o website."""
        if origin not in {"PLANILHA_LOCAL", "brasilapi"} or incoming.cnpj != cnpj:
            raise ValueError("Origem ou identidade empresarial inválida")

        def persist():
            row = self.row(cnpj)
            previous = json.loads(row["company"] or "{}")
            merged = asdict(Company(cnpj)) | previous
            before = dict(merged)
            now = time.time()
            for field, value in asdict(incoming).items():
                if field == "cnpj" or not value or not str(value).strip():
                    continue
                if overwrite or not merged.get(field):
                    merged[field] = value
                    self.conn.execute("INSERT OR REPLACE INTO company_field_sources VALUES (?,?,?,?,?)",
                                      (cnpj, field, origin, import_id, now))
            sources = ";".join(dict.fromkeys(v for v in (row["source"] + ";" + origin).split(";") if v))
            self.conn.execute("UPDATE companies SET company=?,source=?,updated_at=? WHERE cnpj=?",
                              (json.dumps(merged, ensure_ascii=False), sources, now, cnpj))
            identity_changed = any(before.get(k) != merged.get(k) for k in ("legal_name", "trade_name", "city", "state"))
            if origin == "PLANILHA_LOCAL" and identity_changed:
                # Identidade diferente exige nova validação; conservar URL como candidato, consumo e tentativas.
                candidates = json.loads(row["candidates"] or "[]")
                validation = json.loads(row["validation"] or "{}")
                if validation.get("website") and validation["website"] not in candidates:
                    candidates.append(validation["website"])
                self.conn.execute("""UPDATE companies SET validation=NULL,candidates=?,confidence='PENDENTE',
                                     status='PENDENTE',error='' WHERE cnpj=?""", (json.dumps(candidates), cnpj))
            return Company(**merged)

        if self.conn.in_transaction:
            return persist()
        with self.transaction():
            return persist()

    def row(self, cnpj):
        return self.conn.execute("SELECT * FROM companies WHERE cnpj=?", (cnpj,)).fetchone()

    def rows(self, ids):
        return [dict(self.row(cnpj)) | {"company_sources": self.company_sources(cnpj),
                "discovery_audit": self.get_state("discovery_audit:" + cnpj, {})} for cnpj in ids]

    def update(self, cnpj, **fields):
        allowed = {"company", "validation", "candidates", "source", "status", "confidence", "attempts", "error"}
        if not fields.keys() <= allowed:
            raise ValueError("Campos desconhecidos")
        encoded = {k: json.dumps(v, ensure_ascii=False) if k in {"company", "validation", "candidates"} else v
                   for k, v in fields.items()}
        encoded["updated_at"] = time.time()
        self.conn.execute(f"UPDATE companies SET {','.join(k+'=?' for k in encoded)} WHERE cnpj=?",
                          (*encoded.values(), cnpj))

    def cache_get(self, provider, key):
        row = self.conn.execute("SELECT value FROM cache WHERE provider=? AND key=? AND expires_at>?",
                                (provider, key, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def cache_put(self, provider, key, value, ttl):
        self.conn.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?)",
                          (provider, key, json.dumps(value, ensure_ascii=False), time.time() + ttl))

    def get_state(self, key, default=None):
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def usage(self, since=0):
        return {row["provider"]: dict(row) for row in self.conn.execute("""
          SELECT provider, COUNT(*) AS requisicoes,
          SUM(CASE WHEN outcome='SUCESSO' THEN 1 ELSE 0 END) AS sucessos,
          SUM(CASE WHEN outcome NOT IN ('SUCESSO','RESERVADA') THEN 1 ELSE 0 END) AS falhas,
          SUM(CASE WHEN outcome='RESERVADA' THEN 1 ELSE 0 END) AS incertas
          FROM requests WHERE started_at>=? GROUP BY provider""", (since,))}
