# Busca CNPJs — continuidade do projeto

Situação em **10/11/2026**. Este arquivo resume o que foi feito, o que está publicado e o que falta, para retomar sem refazer etapas. Nenhum segredo está aqui: credenciais ficam só nos arquivos privados citados.

## 1. Resumo

- A pesquisa real funciona de ponta a ponta (cadastro → busca Serper → validação de sites) e foi comprovada no piloto da WEG.
- Produção está com a **pesquisa externa desligada**. A equipe ainda **não** foi liberada.
- PR #8 (robots.txt RFC 9309, robô DazumCNPJ, painel do Google, usuário no cabeçalho, mensagens distintas) está em produção (`db54379`).
- Em 10/11 o proprietário definiu a **regra final de tokens** (pesquisa real concluída = 1 token; resultado já salvo reaproveitado sem custo), pediu as categorias **site da matriz** e **indicado pelo painel do Google**, o saldo antes/depois e **autorizou a etapa 1a** com 5 CNPJs. Essas mudanças estão na branch `feat/finalizacao-operacional`.
- Próximo marco: integrar essa branch, publicar com a pesquisa desligada e ativar a etapa 1a.

## 2. GitHub

| Item | Valor |
|---|---|
| Repositório | https://github.com/DazumDigital/busca_cnpjs |
| `main` | `b72a77b` — PR #9 (inclui PRs #5 a #8) |
| Branch em andamento | `feat/qualidade-sites` (descoberta e classificação de sites após a etapa 1a) |
| PR a abrir | https://github.com/DazumDigital/busca_cnpjs/pull/new/feat/qualidade-sites |

Histórico desta fase: `34ba433` (#5, validação de sites resiliente e comandos administrativos), `8e9955e` (#6, RFC 9309 e achados do piloto), `1a73e37` (#7, revalidação sem novo token, crawl-delay por site e lotes em rodízio), `1acad01`/`4961f9a` (#8, operação da equipe e documentação).

O push só funciona no **terminal interativo do proprietário** (Git Credential Manager). O `gh` não está instalado; PRs são abertos e integrados pela interface do GitHub.

## 3. Vercel

| Item | Valor |
|---|---|
| Projeto | `dazum-digital/busca-cnpjs` (Team ID `team_BzXgymmWBHc9JV86DWRhQJAk`, Project ID `prj_AaNc6gH2J5nhtFo5bbMMp5giYf7F`) |
| Produção | `dpl_2nvc7HnnoPePtvcgQpBSbD5CuE51`, código `b72a77b` (PR #9), publicado em 10/11 11:32 BRT com a pesquisa desligada |
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

## 6b. Etapa 1a (10/11/2026) — primeiro teste real da equipe

- 5 CNPJs pesquisados pelo proprietário com a conta `cristiano` (Apple individual; Angeloni, Giassi, FG Prime e Melau Pet em lote): 5 concluídas, **5 tokens** (equipe 2.469 → **2.464**), sem duplicidade; Serper 5, Minha Receita 5, sites 22 (tetos 5/5/40). Logs sem erros. Encerrada com bloqueio no Neon e flag `false` (deploy `dpl_2nvc7HnnoPePtvcgQpBSbD5CuE51`).
- Nenhum site oficial confirmado. Causas: CNPJ de outro estabelecimento da mesma raiz recusado (Apple); Waze/ABINEE/Pandapé tratados como candidatos; falha de conexão sem diagnóstico e domínio principal fora dos resultados (Angeloni); só diretórios na busca (FG Prime); busca com o artigo "A" e sem "CAETANO" (Melau Pet). Giassi (filial) corretamente como site da matriz.
- Correções na branch `feat/qualidade-sites` (descoberta e classificação; ver guia de ativação). Validadas com os links reais guardados e fontes simuladas (`tests/test_web_qualidade_sites.py`); falta comprovação real.

## 7. Decisões técnicas

1. **Falha de site não derruba a consulta**: até 3 sites candidatos e 8 acessos; sem confirmação, o status é `FALHA_VALIDACAO_SITE_OFICIAL` e o motivo aparece no candidato.
2. **robots.txt pela RFC 9309**: lê 500 KiB de qualquer tipo de conteúdo; 4xx = sem regras; 5xx = encerra o site; guarda só as regras de `*` e `DazumCNPJ`.
3. **Sem evasão**: User-Agent `DazumCNPJ/1.0 (+https://busca-cnpjs.vercel.app/robo)`, página pública `/robo`; 403/429/503 encerram o site.
4. **Crawl-delay por site** (mínimo 10 s) e lotes em **rodízio**: um item em espera não trava os demais.
5. **Painel do Google**: o site indicado é validado primeiro e marcado "Indicado pelo painel do Google", nunca oficial; só a URL é guardada.
6. **Tokens** (regra final de 10/11, substitui a de 09/11): 1 token por pesquisa real concluída, com ou sem links; resultado já salvo reaproveitado sem custo e com aviso; falhas, bloqueios, reabertura e revalidação não cobram; o livro de tokens por consulta impede débito duplicado.
11. **Site da matriz** para filiais e **grupo do painel do Google**, ambos sem confirmação automática; colunas novas no fim das exportações.
12. **Lotes** dependem da página aberta (fila persistente no Neon, retomada sem perdas); processamento em segundo plano avaliado e pendente de decisão (ver guia de ativação).
7. **Mensagens distintas**: `PESQUISA_NAO_LIBERADA` (403), `PESQUISA_SUSPENSA`, `INTEGRACAO_NAO_VERIFICADA`, `LIMITE_CONSULTAS` (429), `COTA_EXTERNA_ESGOTADA`; esperas longas mostram "limite do período".
8. **Revalidação no lugar** (`revalidate-sites`): refaz só os sites com os links já obtidos e atualiza a mesma consulta.
9. **Limite temporário 429** do cadastro/busca pausa a etapa (máx. 3 vezes) em vez de falhar.
10. **Liberação gradual** aprovada (seção 8 e `docs/ATIVAR_PESQUISA_EXTERNA.md`).

## 8. Pendências e próximos passos

0. **Integrar `feat/qualidade-sites`** e publicar com a pesquisa desligada. Comprovar com uma **revalidação dos 5 CNPJs da etapa 1a** (`revalidate-sites`, só acessos a sites: sem Serper, sem cadastro e sem token), mediante autorização.
1. ~~Integrar `feat/finalizacao-operacional`~~ (PR #9, em produção desde 10/11).
2. **Etapa 1a — validação com 5 CNPJs** (autorizada em 10/11: 00.623.904/0001-73, 83.646.984/0001-00, 83.648.477/0016-91 [filial], 04.743.028/0001-70, 66.136.840/0001-94; até 5 Serper, 5 cadastros, 40 acessos a sites e 5 tokens; Serper confirmada em 10/11 com 2.467 créditos, sem cartão, sem pagamentos e sem recarga; validade de 7 dias):
   - rascunho do manifesto em `%USERPROFILE%\.dazum-secrets\etapa1a-manifest.RASCUNHO.json`;
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
