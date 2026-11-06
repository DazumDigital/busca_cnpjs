"""Importação por streaming, preservando a ordem e todas as linhas de auditoria."""
import csv
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook

from app.models import Company


def normalize_cnpj(value) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return ""
        value = int(value)
    raw = str(value).strip()
    if not re.fullmatch(r"[0-9./\-\s]+", raw):
        return ""
    digits = re.sub(r"[^0-9]", "", raw)
    return digits.zfill(14) if 1 <= len(digits) <= 14 else ""


def valid_cnpj(value: str) -> bool:
    if not re.fullmatch(r"[0-9]{14}", value) or len(set(value)) == 1:
        return False
    numbers = [int(v) for v in value]
    for size, weights in ((12, (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)),
                          (13, (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))):
        remainder = sum(a * b for a, b in zip(numbers[:size], weights)) % 11
        if numbers[size] != (0 if remainder < 2 else 11 - remainder):
            return False
    return True


def header_key(value):
    raw = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", raw)


BUSINESS_ALIASES = {
    "legal_name": {"razaosocial", "nomeempresarial", "razaosocialnomeempresarial", "razaosocialnome", "razaosocialdaempresa"},
    "trade_name": {"nomefantasia", "fantasia", "nomecomercial"},
    "city": {"municipio", "cidade", "nomedomunicipio", "nomemunicipio", "nomecidade"},
    "state": {"uf", "estado", "unidadefederativa", "unidadedafederacao", "siglauf"},
    "registration_status": {"situacaocadastral", "situacaodocnpj", "descricaosituacaocadastral", "situacaocadastro", "situacao"},
}


def business_text(value):
    if value is None or isinstance(value, bool) or (isinstance(value, float) and not math.isfinite(value)):
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if text.startswith("=") or header_key(text) in {"", "nan", "none", "null", "na", "naoinformado"}:
        return ""
    return text


@dataclass
class ImportResult:
    selected: list[tuple[str, str]]
    audit: list[dict]
    counts: dict
    companies: dict[str, Company] = field(default_factory=dict)
    business_columns: dict[str, str] = field(default_factory=dict)


def import_file(path: Path, limit=1000, column: str | None = None) -> ImportResult:
    if not 1 <= limit <= 1000:
        raise ValueError("Limite de importação deve ser 1..1000")
    counts = dict(importados=0, validos=0, invalidos=0, duplicados=0, fora_limite=0, selecionados=0)
    selected, audit, seen = [], [], set()
    companies, business_columns = {}, {}

    def consume(rows):
        headers = next(rows, None)
        if headers is None:
            raise ValueError("Arquivo vazio")
        keys = [header_key(h) for h in headers]
        options = [i for i, k in enumerate(keys) if k == header_key(column)] if column else [
            i for i, k in enumerate(keys) if "cnpj" in k]
        if len(options) != 1:
            raise ValueError("Coluna CNPJ ausente ou ambígua; informe --cnpj-column")
        index = options[0]
        mapped = {}
        for field_name, aliases in BUSINESS_ALIASES.items():
            matches = [i for i, key in enumerate(keys) if key in aliases]
            if len(matches) > 1:
                raise ValueError(f"Colunas empresariais ambíguas para {field_name}: revise os cabeçalhos")
            if matches:
                mapped[field_name] = matches[0]
                business_columns[field_name] = str(headers[matches[0]])
        for line, row in enumerate(rows, 2):
            counts["importados"] += 1
            raw = row[index] if index < len(row) else None
            original = "" if raw is None else str(raw)
            cnpj = normalize_cnpj(raw)
            if not valid_cnpj(cnpj):
                kind = "INVALIDO"
                counts["invalidos"] += 1
            else:
                counts["validos"] += 1
                if cnpj in seen:
                    kind = "DUPLICADO"
                    counts["duplicados"] += 1
                else:
                    seen.add(cnpj)
                    if len(selected) < limit:
                        kind = "SELECIONADO"
                        selected.append((cnpj, original))
                        values = {name: business_text(row[i]) if i < len(row) else "" for name, i in mapped.items()}
                        if values.get("state"):
                            values["state"] = values["state"].upper()
                        if any(values.values()):
                            companies[cnpj] = Company(cnpj=cnpj, **values)
                    else:
                        kind = "FORA_LIMITE"
                        counts["fora_limite"] += 1
            audit.append(dict(linha=line, original=original, cnpj=cnpj, classificacao=kind))

    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            sample = stream.read(8192)
            stream.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            consume(iter(csv.reader(stream, dialect)))
    elif path.suffix.lower() == ".xlsx":
        book = load_workbook(path, read_only=True, data_only=False)
        try:
            consume(iter(book.active.iter_rows(values_only=True)))
        finally:
            book.close()
    else:
        raise ValueError("Formato aceito: CSV UTF-8 ou XLSX (primeira aba)")
    counts["selecionados"] = len(selected)
    return ImportResult(selected, audit, counts, companies, business_columns)
