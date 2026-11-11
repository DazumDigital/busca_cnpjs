# Busca CNPJs — continuidade do projeto

Situação em **11/11/2026**. Este arquivo resume o estado real do sistema, o que está publicado e o que falta, para retomar sem refazer etapas. Nenhum segredo está aqui: credenciais ficam só nos arquivos privados citados.

Documentos relacionados:

- [`HOMOLOGACAO_2026-11.md`](HOMOLOGACAO_2026-11.md): auditoria da revalidação, testes e prontidão.
- [`DEMONSTRACAO.md`](DEMONSTRACAO.md): roteiro da apresentação com resultados salvos.
- [`ATIVAR_PESQUISA_EXTERNA.md`](ATIVAR_PESQUISA_EXTERNA.md): ativação, bloqueio, auditoria e recuperação.

## 1. Resumo

- A plataforma está publicada em https://busca-cnpjs.vercel.app com login, saldo, consulta individual, lotes de até 15, histórico e exportação CSV/XLSX.
- A **pesquisa externa está desligada** (flag `false` e política bloqueada no Neon). Com ela desligada, a equipe consulta e exporta os **25 resultados já salvos**, sem custo.
- A pesquisa real já foi comprovada em produção:
  - piloto WEG (09/11);
  - etapa 1a com 5 CNPJs (10/11);
  - revalidação só de sites desses 5 (10/11), com o resultado aprovado pelo proprietário.
- A liberação operacional da equipe (etapa 1b em diante) depende de **nova autorização** e da conferência do Billing da Serper.

## 2. GitHub

| Item | Valor |
|---|---|
| Repositório | https://github.com/DazumDigital/busca_cnpjs |
| `main` | `cccc173` — PR #11 (homologação para produção); branch `fix/reserva-serper` com a reserva mínima de 100 créditos |
| Branch desta homologação | `fix/homologacao-producao` (correções de integridade, testes e documentação) |

Histórico: PRs #5 a #8 (validação resiliente, RFC 9309, robô DazumCNPJ, operação), #9 (regra final de tokens, site da matriz, painel do Google), #10 (qualidade da descoberta de sites).

O push só funciona no **terminal interativo do proprietário** (Git Credential Manager). O `gh` não está instalado; PRs são abertos e integrados pela interface do GitHub.

## 3. Vercel

| Item | Valor |
|---|---|
| Projeto | `dazum-digital/busca-cnpjs` (Team ID `team_BzXgymmWBHc9JV86DWRhQJAk`, Project ID `prj_AaNc6gH2J5nhtFo5bbMMp5giYf7F`), plano **Hobby** |
| Produção | `dpl_FqaYCVJ8XRBeuw2wExdQ3jhdv4Fc` (11/11 14:23 BRT, redeploy feito pelo proprietário de `dpl_D7Ay5uHawzEFu1GfjEdytKwPh24q`, código `cccc173`) |
| Publicações de 10/11 | `dpl_9NeP7tPDyNnFGST4o2Wpkusak7BM` (14:06, flag false), `dpl_DydBZRWicDMAqdxKDZNfzgsZzqPs` (14:21, flag true só para a revalidação), `dpl_Dt1FFA1kiwZXUT3kCZyzcm9yvPDD` (14:39, flag false) |
| `WEB_ENABLE_EXTERNAL` | `true` em Production desde 11/11 (alterado pelo proprietário); `false` em Preview. A política do Neon segue bloqueada até a aprovação da etapa 1b |
| `SERPER_API_KEY` | Secret **só em Production** |
| Deploy automático por push | Desligado (`vercel.json`: `git.deploymentEnabled=false`) |
| Função | Python (FastAPI), `maxDuration` 30 s |

Deploys são feitos pelo CLI a partir da `main` limpa (comandos na seção 10). Depois de cada deploy, conferir:

- `/api/health` = 200 (conexão com o Neon);
- `/api/lotes` e `/api/consultas` sem sessão = 401;
- `/robo` = 200;
- arquivos estáticos idênticos ao commit;
- cabeçalhos de segurança presentes;
- logs sem erros.

Logs da CLI no Hobby: só a última hora, e trazem apenas a saída das funções (zero registros = nenhum erro registrado).

## 4. Neon (leitura de 10/11 14:40 BRT, sessão somente leitura)

**Organização `dazumdigital`:**
- 2.469 tokens iniciais, **5 consumidos** (etapa 1a), 0 reservados, **2.464 livres**;
- **25 empresas salvas**: 20 históricas e 5 da etapa 1a;
- livro de tokens com 5 lançamentos, um por consulta, sem duplicidade;
- consultas: 5 concluídas e 8 não concluídas. As não concluídas são itens de lote com a pesquisa desligada (`EMPRESA_NAO_PESQUISADA`), sem custo;
- usuário `cristiano` ativo.

**Organizações de QA:**
- `qa-serper-weg`: 1 token, 1 consumido;
- `qa-publicacao-bb4031e695b5`: 3 tokens, 3 consumidos;
- os usuários das duas estão desativados.

**Livro técnico (acumulado):** Serper 6, Minha Receita 6, sites 41.

**Política externa:**
- **bloqueada** (`blocked: true`);
- escopo: só `dazumdigital` e os 5 CNPJs da etapa 1a;
- tetos: Serper 6, Minha Receita 6 (iguais ao livro), sites 70;
- confirmação da Serper registrada em 10/11 13:52 UTC, válida por 168 h (**até 17/11 10:52 BRT**).

## 5. Serper

- Saldo **2.467** confirmado pelo proprietário em 10/11, antes da etapa 1a: sem cartão, sem pagamentos, recarga automática desativada.
- A etapa 1a fez 5 buscas; a revalidação não fez nenhuma. O saldo esperado agora é **2.462**. **Conferir no Billing** antes de qualquer nova ativação.
- A chave da Vercel é a mesma do `.env` da POC (conferido no piloto pela queda de exatamente 1 crédito).

## 6. Etapas reais já executadas

| Etapa | Data | Escopo | Chamadas | Tokens | Resultado |
|---|---|---|---|---|---|
| Piloto WEG | 09/11 | `qa-serper-weg`, 1 CNPJ | Serper 1 · cadastro 1 · sites 8 | 1 (QA) | Sem site oficial: `weg.net` responde 403 a robôs |
| Etapa 1a | 10/11 | `dazumdigital`, 5 CNPJs | Serper 5 · cadastro 5 · sites 22 | 5 | Nenhum oficial confirmado; causas corrigidas no PR #10 |
| Revalidação de sites | 10/11 14:24–14:34 | os mesmos 5 | Serper 0 · cadastro 0 · sites 11 | 0 | Apple e Melau Pet oficiais; Giassi site da matriz; Angeloni possíveis sites; FG Prime referências |

Detalhes e a aprovação do proprietário estão em [`HOMOLOGACAO_2026-11.md`](HOMOLOGACAO_2026-11.md).

## 7. Regras definitivas

**Tokens:**
- 1 token por pesquisa real concluída;
- resultado já salvo é reaproveitado sem custo, com aviso;
- falhas, bloqueios, reabertura, recarga e revalidação nunca cobram;
- o livro de tokens por consulta impede débito duplicado;
- o saldo aparece antes e depois de cada pesquisa.

**Resultados** (aprovados em 10/11):
- priorizar o site oficial confirmado;
- sem confirmação, preservar possíveis sites, site da matriz (filial), indicação do painel do Google, redes sociais e referências de terceiros úteis;
- diretório ou plataforma nunca vira site oficial ou possível site;
- um link útil não é descartado só porque a titularidade não pôde ser comprovada.

**Reserva da Serper:** o saldo nunca fica abaixo de 100 créditos. A busca só é reservada se, depois dela, restarem 100 créditos do saldo confirmado; senão, a pesquisa é recusada com mensagem clara, sem chamada e sem token (`RESERVA_SERPER`).

**Pesquisa pendente com a pesquisa externa indisponível** (desta homologação): a consulta é encerrada sem custo e a reserva volta. Um resultado salvo é entregue sem custo. O progresso pago fica guardado e uma nova consulta continua dele.

**Falha transitória do cadastro** (Minha Receita, gratuito): uma nova consulta tenta de novo, no máximo uma vez por consulta e dentro das cotas. Falhas depois da busca paga continuam exigindo revisão (`retry-job`).

**Lotes:**
- persistentes no Neon;
- avançam com a página aberta, em rodízio;
- fechar a página pausa sem perder nada, e reabrir pelo histórico retoma;
- **não há processamento em segundo plano**.

## 8. Pendências e próximos passos

1. **Integrar `fix/homologacao-producao`** e publicar com a pesquisa desligada. Verificar domínio, Neon e logs como na seção 3.
2. **Contas individuais da equipe**. O proprietário cria uma por pessoa, com a senha pedida no terminal: `create-user nome.sobrenome --org dazumdigital`. Evita o limite de 5 logins por minuto por usuário e mantém o histórico de cada um.
3. **Conferir o Billing da Serper** (esperado 2.462, sem cartão, sem recarga) e autorizar a **etapa 1b**:
   - escopo `dazumdigital`, qualquer CNPJ;
   - Serper +30 (2/min, 20/h, 30/dia), cadastro +60, sites +240;
   - tetos acumulados com o livro: Serper 36, Minha Receita 66, sites 281.
4. **Homologar um lote real** de até 15 CNPJs novos na etapa 1b e então aplicar a **etapa 2**: Serper 500 (5/min, 60/h, 150/dia), Minha Receita 1.000 (10/min, 120/h, 300/dia), sites 4.000 (30/min, 600/h, 2.000/dia).
5. **Rotina semanal** com a pesquisa ligada: reconferir o Billing e rodar `configure-external` (a confirmação vale 168 h).
6. **Plano da Vercel**: o Hobby é para uso pessoal e não comercial. O uso operacional pela empresa exige plano ou condição adequada (ex.: Pro, pago) ou outra hospedagem. É uma decisão do proprietário; nada foi contratado.
7. Em aberto, sem prazo:
   - processamento de lotes sem a aba aberta. Opções: Vercel Schedules a cada 15 min (sem custo, lento) ou plano Pro (pago); decisão do proprietário;
   - CNPJ alfanumérico;
   - páginas acima de 1 MB (ex.: `www.angeloni.com.br`) não são analisadas e ficam como possível site com o motivo.

## 9. Testes desta versão

- Python: 436 testes, incluindo concorrência e transações em PostgreSQL local descartável.
- Frontend: 17 testes.
- Build: 17 assets.
- Nenhum teste acessa a rede (fontes simuladas).

Também foram feitos testes funcionais na interface local com fontes simuladas e com as 25 empresas salvas copiadas do Neon:
- consulta reaproveitada sem custo;
- pesquisa nova com 1 token;
- lote de 15 resultados salvos concluído com 0 token;
- exportações CSV e XLSX;
- celular em 375 px, sem rolagem horizontal.

## 10. Comandos para retomar (PowerShell)

```powershell
Set-Location 'C:\Cristiano\busca_cnpjs\outputs\busca-cnpjs-git'
git status --short --branch
git fetch origin --prune
git log --oneline -5 origin/main
$python = 'C:\Cristiano\busca_cnpjs\outputs\cnpj-enrichment\.venv\Scripts\python.exe'
$envfile = "$env:USERPROFILE\.dazum-secrets\cnpj-web-neon.env"
```

Testes (sem rede e sem credenciais de produção):

```powershell
$testTemp = Join-Path (Get-Location) ('.pytest_cache\run-' + [guid]::NewGuid().ToString('N'))
& $python -m pytest -q --basetemp $testTemp
node --test tests/web_core.test.mjs tests/web_batch.test.mjs
& $python -m app.web.build
git diff --check
```

Concorrência em PostgreSQL **local e descartável** (nunca o Neon): inicie um cluster em `127.0.0.1:55445` com o banco `quota_qa` e rode com `$env:WEB_QUOTA_TEST_DATABASE_URL='postgresql+psycopg://postgres@127.0.0.1:55445/quota_qa'`.

Estado operacional (só leitura; não chama provedores):

```powershell
& $python -m app.web.manage --env-file $envfile external-status
& $python -m app.web.manage --env-file $envfile balance --org dazumdigital
```

Push e deploy:

```powershell
git -C 'C:\Cristiano\busca_cnpjs\outputs\busca-cnpjs-git' push -u origin NOME_DA_BRANCH
$env:VERCEL_ORG_ID = 'team_BzXgymmWBHc9JV86DWRhQJAk'
$env:VERCEL_PROJECT_ID = 'prj_AaNc6gH2J5nhtFo5bbMMp5giYf7F'
npx --yes vercel@65.12.1 deploy --prod --yes --scope dazum-digital
npx --yes vercel@65.12.1 inspect busca-cnpjs.vercel.app --scope dazum-digital
```

Desligar a pesquisa imediatamente (banco), se necessário: `& $python -m app.web.manage --env-file $envfile block-external`.

## 11. Cuidados neste ambiente

- **PowerShell 5.1** corrompe acentos ao regravar arquivos (`Get-Content`/`Set-Content`). Edite com o editor ou com Python e faça os commits com `git commit -F arquivo`.
- O **Avast** intercepta HTTPS nesta máquina. Scripts Python locais que acessam sites devem usar o repositório de certificados do Windows (`ssl.create_default_context()`); isso não afeta a Vercel.
- O teste de produção com senha é feito pelo proprietário na interface. Credenciais nunca vão para o Git, logs ou chat.
