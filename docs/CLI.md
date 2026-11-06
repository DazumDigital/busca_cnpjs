# Motor de pesquisa por terminal

O pacote original continua disponível por `python -m app.main` e pelo comando `cnpj-enrichment`. A interface web acrescenta autenticação, saldo comercial e apresentação de resultados; não substitui as rotinas CLI.

Instalação mínima do motor, sem dependências web:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m app.main --help
.\.venv\Scripts\python.exe -m app.main --check-config
```

Importação e exportação offline de exemplo, sem consultas:

```powershell
.\.venv\Scripts\python.exe -m app.main --input examples/cnpjs.csv --limit 10 --dry-run
```

O exemplo contém inscrições geradas para testes, não empresas destinadas a consultas reais. `--dry-run` não inventa resultados: importa, audita e exporta registros pendentes em arquivos próprios.

Rotinas existentes:

| Módulo | Uso |
| --- | --- |
| `app.main` | Importação, processamento controlado e exportação |
| `app.pilot3` | Preparar, verificar e executar pilotos autorizados de até três empresas |
| `app.new_batch` | Selecionar inscrições inéditas sem pesquisar |
| `app.reclassify` | Reclassificação offline em cópia nova |
| `app.review_saved` | Revisão de candidatos/evidências já armazenados |
| `app.consolidate` | Consolidação com fontes explícitas e referências úteis |

Execute `--help` em cada módulo para os argumentos completos. Os arquivos operacionais, relatórios históricos, entradas reais e bancos da POC permanecem locais e não são distribuídos neste repositório.

Execuções reais exigem autorização, configuração de conta gratuita sem cobrança automática, saldo confirmado, manifesto válido e orçamento adicional zero. Nunca reinicie cotas criando outro banco de consumo. Não execute um lote grande sem revisar os pilotos anteriores. CNPJ alfanumérico permanece fora do escopo desta versão numérica.
