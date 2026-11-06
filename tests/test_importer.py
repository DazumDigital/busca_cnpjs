import csv

import pytest
from openpyxl import Workbook

from app.services.importer import import_file, normalize_cnpj, valid_cnpj
from conftest import generated_cnpj


@pytest.mark.parametrize("raw,normalized", [
    ("00.000.000/0001-91", "00000000000191"), (191, "00000000000191"),
    (191.0, "00000000000191"), (None, ""), (True, ""), ("foo191", ""),
    ("1e13", ""), (float("nan"), ""), (1.5, ""), ("123456789012345", ""),
])
def test_normalization(raw, normalized):
    assert normalize_cnpj(raw) == normalized


@pytest.mark.parametrize("cnpj,valid", [("00000000000191", True), ("00000000000000", False),
                                       ("11111111111111", False), ("00000000000192", False), ("", False)])
def test_check_digits(cnpj, valid):
    assert valid_cnpj(cnpj) == valid


def test_csv_audit_and_order(tmp_path):
    path = tmp_path / "data.csv"
    one, two = generated_cnpj(1), generated_cnpj(2)
    path.write_text(f"Nome;CNPJ\nA;{one}\nB;{two}\nC;{one}\nD;invalido\n", encoding="utf-8-sig")
    result = import_file(path, 1)
    assert result.selected == [(one, one)]
    assert result.counts == dict(importados=4, validos=3, invalidos=1, duplicados=1, fora_limite=1, selecionados=1)
    assert [v["classificacao"] for v in result.audit] == ["SELECIONADO", "FORA_LIMITE", "DUPLICADO", "INVALIDO"]


def test_excel_numeric_zeros_and_formulas(tmp_path):
    path = tmp_path / "data.xlsx"
    book = Workbook()
    book.active.append(["Inscrição CNPJ"])
    book.active.append([191])
    book.active.append(["=1+1"])
    book.save(path)
    book.close()
    result = import_file(path)
    assert result.selected[0][0] == "00000000000191"
    assert result.audit[0]["original"] == "191"
    assert result.counts["invalidos"] == 1


def test_cap_1000_and_invalids_before_selection(tmp_path):
    path = tmp_path / "large.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["CNPJ"])
        writer.writerow(["invalid"])
        writer.writerows([generated_cnpj(i)] for i in range(1, 1006))
    result = import_file(path)
    assert len(result.selected) == 1000
    assert result.selected[0][0] == generated_cnpj(1)
    assert result.selected[-1][0] == generated_cnpj(1000)
    assert result.counts["fora_limite"] == 5
    with pytest.raises(ValueError):
        import_file(path, 1001)


def test_ambiguous_column(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text("CNPJ;CNPJ matriz\n191;191", encoding="utf-8")
    with pytest.raises(ValueError):
        import_file(path)
    assert import_file(path, column="CNPJ").counts["selecionados"] == 1
