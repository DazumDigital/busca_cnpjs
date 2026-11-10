# Busca CNPJs — continuidade do projeto

Situação em **10/11/2026**. Este arquivo resume o que foi feito, o que está publicado e o que falta, para retomar sem refazer etapas. Nenhum segredo está aqui: credenciais ficam só nos arquivos privados citados.

## 1. Resumo

- A pesquisa real funciona de ponta a ponta (cadastro → busca Serper → validação de sites) e foi comprovada no piloto da WEG.
- Produção está com a **pesquisa externa desligada**. A equipe ainda **não** foi liberada.
- Os ajustes aprovados em 09/11 (robots.txt RFC 9309, robô DazumCNPJ, painel do Google, usuário no cabeçalho, mensagens distintas e nova regra de tokens) estão na branch `feat/operacao-equipe`, testados, **aguardando PR, merge e deploy**.
- Próximo marco: etapa 1a da liberação gradual, com 5 CNPJs inéditos escolhidos pelo proprietário.

## 2. GitHub

| Item | Valor |
|---|---|
| Repositório | https://github.com/DazumDigital/busca_cnpjs |
| `main` | `d70b1e4` — PR #7 (inclui PRs #5 e #6) |
| Branch em andamento | `feat/operacao-equipe` (commit `1acad01` + documentação), já enviada ao GitHub |
| PR a abrir | https://github.com/DazumDigital/busca_cnpjs/pull/new/feat/operacao-equipe |

Histórico desta fase: `34ba433` (#5, validação de sites resiliente e comandos administrativos), `8e9955e` (#6, RFC 9309 e achados do piloto), `1a73e37` (#7, revalidação sem novo token, crawl-delay por site e lotes em rodízio), `1acad01` (operação da equipe, ainda fora da `main`).

O push só funciona no **terminal interativo do proprietário** (Git Credential Manager). O `gh` não está instalado; PRs são abertos e integrados pela interface do GitHub.

## 3. Vercel

| Item | Valor |
|---|---|
| Projeto | `dazum-digital/busca-cnpjs` (Team ID `team_BzXgymmWBHc9JV86DWRhQJAk`, Project ID `prj_AaNc6gH2J5nhtFo5bbMMp5giYf7F`) |
| Produção | `dpl_6iZ39ReqYCNFpzfPnejnp3xKFpN1`, código `d70b1e4`, publicado em 09/11 15:47 BRT |
| `WEB_ENABLE_EXTERNAL` | `false` em Production (entrada própria, tipo Config); `false` em Preview (entrada separada) |
| `SERPER_API_KEY` | Secret **só em Production** |
| Deploy automático por push | Desligado (`vercel.json`: `git.deploymentEnabled=false`) |

Deploys são feitos pelo CLI a partir da `main` limpa (comando na seção 9). Depois de cada deploy, conferir `/api/health` = 200, `/api/lotes` sem sessão = 401 e os arquivos estáticos idênticos ao commit.

## 4. Neon (leitura de 10/11, sessão somente leitura)

- `dazumdigital`: **2.469 tokens livres**, 0 consumidos, 0 reservados; **20 empresas históricas** intactas; usuário `cristiano` ativo.
- `qa-serper-weg` (piloto): 1 token, 1 consumido; 1 empresa (WEG); usuário `qa.piloto.weg` **desativado**.
- `qa-publicacao-bb4031e695b5`: organização de QA anterior, 3 tokens consumidos, usuário desativado.
- Livro técnico: Serper 1, Minha Receita 1, sites 8 (todas do piloto WEG). Última reserva: 09/11 18:43 UTC.
- Política externa: **bloqueada** (`blocked: true`), ainda com o escopo do piloto (só `qa-serper-weg` e o CNPJ da WEG).

## 5. Serper

- Saldo **2.467** créditos, confirmado pelo proprietário no Billing após o piloto (2.468 → 2.467, exatamente a chamada registrada).
- Na conferência de 09/11 16:24 UTC: sem cartão, sem pagamentos, recarga automática desmarcada e desabilitada.
- A chave da Vercel é a mesma do `.env` da POC; a queda de 1 crédito confirmou que pertence a essa conta.
- A confirmação registrada na política vale 24 h e **já venceu**. Antes de religar, o proprietário deve conferir o Billing de novo.

## 6. Piloto WEG (84.429.695/0001-11)

- Cadastro: WEG SA · WEG · Jaraguá do Sul/SC · Ativa. Busca: 10 links.
- Validação: `www.weg.net` respondeu **HTTP 403** (proteção anti-robô para IPs de datacenter); `weg.gupy.io` tinha um robots.txt que o leitor antigo recusava (corrigido em `1acad01`); a página da IECEE é de outra empresa e deixou de aparecer como possível site.
- Resultado salvo: sem site oficial confirmado; possíveis sites `www.weg.net` (HTTP 403) e `weg.gupy.io`; redes sociais LinkedIn e Instagram.
- A revalidação de sites usou 5 acessos, sem Serper, sem cadastro e sem novo token.

## 7. Decisões técnicas

1. **Falha de site não derruba a consulta**: até 3 sites candidatos e 8 acessos; sem confirmação, o status é `FALHA_VALIDACAO_SITE_OFICIAL` e o motivo aparece no candidato.
2. **robots.txt pela RFC 9309**: lê 500 KiB de qualquer tipo de conteúdo; 4xx = sem regras; 5xx = encerra o site; guarda só as regras de `*` e `DazumCNPJ`.
3. **Sem evasão**: User-Agent `DazumCNPJ/1.0 (+https://busca-cnpjs.vercel.app/robo)`, página pública `/robo`; 403/429/503 encerram o site.
4. **Crawl-delay por site** (mínimo 10 s) e lotes em **rodízio**: um item em espera não trava os demais.
5. **Painel do Google**: o site indicado é validado primeiro e marcado "Indicado pelo painel do Google", nunca oficial; só a URL é guardada.
6. **Tokens** (aprovado em 09/11): 1 token só com resultado utilizável (oficial, possível site, referência ou rede social); nada sem resultado, inclusive no histórico; o livro de tokens por consulta impede débito duplicado; revalidação não cobra.
7. **Mensagens distintas**: `PESQUISA_NAO_LIBERADA` (403), `PESQUISA_SUSPENSA`, `INTEGRACAO_NAO_VERIFICADA`, `LIMITE_CONSULTAS` (429), `COTA_EXTERNA_ESGOTADA`; esperas longas mostram "limite do período".
8. **Revalidação no lugar** (`revalidate-sites`): refaz só os sites com os links já obtidos e atualiza a mesma consulta.
9. **Limite temporário 429** do cadastro/busca pausa a etapa (máx. 3 vezes) em vez de falhar.
10. **Liberação gradual** aprovada (seção 8 e `docs/ATIVAR_PESQUISA_EXTERNA.md`).

## 8. Pendências e próximos passos

1. **Integrar `feat/operacao-equipe`**: abrir o PR, integrar na `main`, publicar com a pesquisa desligada e verificar o domínio.
2. **Etapa 1a — validação com 5 CNPJs**:
   - o proprietário escolhe 5 CNPJs inéditos e confere o Billing da Serper;
   - aplicar o manifesto da etapa 1 com `allowed_organizations: ["dazumdigital"]` e `allowed_cnpjs` com os 5 CNPJs;
   - ligar a flag só em Production e publicar;
   - o proprietário pesquisa pela interface com a conta `cristiano`;
   - auditar: dados e fontes no Neon, classificação dos links, tokens (sem duplicidade), histórico, exportações e estabilidade;
   - bloquear de novo até a decisão da etapa 1b.
3. **Etapa 1b — uso inicial**: mesmo manifesto sem `allowed_cnpjs` (30 buscas Serper; 2/min, 20/h, 30/dia).
4. **Homologar um lote real** de até 15 empresas e então aplicar a **etapa 2** (teto aprovado: Serper 500 · 5/min · 60/h · 150/dia; Minha Receita 1.000 · 10/min · 120/h · 300/dia; sites 4.000 · 30/min · 600/h · 2.000/dia).
5. **Rotina semanal**: com `balance_valid_hours: 168`, reconferir o Billing e rodar `configure-external` toda semana.
6. Em aberto, sem prazo: CNPJ alfanumérico (a Minha Receita não documenta suporte); contato opcional na página `/robo`; sites grandes com proteção anti-robô continuarão como "possível site" com o motivo.

## 9. Comandos para retomar (PowerShell)

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

## 10. Cuidados neste ambiente

- **PowerShell 5.1** corrompe acentos ao regravar arquivos (`Get-Content`/`Set-Content`): edite com o editor ou Python e faça commits com `git commit -F arquivo`.
- O **Avast** intercepta HTTPS nesta máquina: scripts Python locais que acessam sites devem usar o repositório de certificados do Windows (`ssl.create_default_context()`); isso não afeta a Vercel.
- O teste de produção com senha é feito pelo proprietário na interface; credenciais nunca vão para o Git, logs ou chat.
