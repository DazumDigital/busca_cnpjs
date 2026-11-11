# Homologação para produção — novembro de 2026

Registro técnico da auditoria da revalidação, das correções desta homologação e da prontidão da plataforma. Dados conferidos no Neon em sessões **somente leitura**; nenhum segredo neste arquivo.

## 1. Revalidação só de sites dos 5 CNPJs da etapa 1a (10/11/2026)

**Autorização e escopo:**
- autorização específica do proprietário no chat;
- refazer só a validação de sites, com os links já guardados na etapa 1a;
- sem Serper, sem cadastro e sem token;
- até 40 acessos.

**Preparação** (somente leitura e DNS local, sem acesso a sites):
- manifesto aplicado: só `dazumdigital` e os 5 CNPJs; Serper e Minha Receita com teto igual ao livro técnico (6/6), portanto nenhuma chamada nova possível; sites com teto 70 (30 já usados + 40);
- `revalidate-sites` nos 5 CNPJs. Para a FG Prime, nenhum domínio deduzido do nome existe no DNS: a pesquisa não foi reaberta e não houve acesso.

**Janela:** flag ligada só em Production (`dpl_DydBZRWicDMAqdxKDZNfzgsZzqPs`). O proprietário reabriu cada consulta concluída pela interface, uma de cada vez.

**Encerramento** (10/11 14:39 BRT):
- `block-external` (política `blocked: true`);
- flag `false` em Production;
- republicação (`dpl_Dt1FFA1kiwZXUT3kCZyzcm9yvPDD`);
- verificações: health 200, rotas protegidas 401, arquivos idênticos ao commit, cabeçalhos de segurança, logs sem erros.

### Auditoria (Neon, somente leitura)

| Item | Antes | Depois | Limite autorizado |
|---|---|---|---|
| Serper | 6 | **6** (0 novas) | 0 |
| Minha Receita | 6 | **6** (0 novas) | 0 |
| Acessos a sites | 30 | **41** (11 novos, todos com sucesso no transporte) | até 40 novos |
| Tokens da equipe | 2.464 livres · 5 consumidos | **2.464 livres · 5 consumidos** | 0 |
| Livro de tokens | 5 lançamentos | **5** | — |
| Empresas salvas | 25 | **25** (as 4 revalidadas atualizadas no lugar) | — |

Acessos por empresa: Apple 2, Angeloni 4, Giassi 3, Melau Pet 2, FG Prime 0. Cada resultado salvo é idêntico ao da consulta e traz a ressalva "Sites revalidados com os links já encontrados, sem nova busca paga e sem novo desconto de token".

### Resultados e conferência do proprietário

| Empresa | Resultado | Avaliação do proprietário |
|---|---|---|
| Apple Computer Brasil (00.623.904/0001-73) | **Site oficial** `www.apple.com/br/`, confirmado pelo CNPJ de outro estabelecimento da mesma empresa (00.623.904/0003-35) | Correto |
| Supermercados Angeloni (83.646.984/0001-00) | **Possíveis sites** `institucional.angeloni.com.br` e `www.angeloni.com.br`. O primeiro encerrou a conexão; o segundo tem página acima de 1 MB | Corretos; manter mesmo sem confirmação |
| Giassi (filial 83.648.477/0016-91) | **Site da matriz** `institucional.giassi.com.br` (CNPJ da matriz 0001-05; filial não aparece); possível site `www.giassi.com.br`; perfil do Facebook; referência do Portal da Transparência | Corretos; manter o site da matriz sem confirmar a filial |
| FG Prime (04.743.028/0001-70) | Sem site oficial; **4 referências** (Econodata, Sr. Watson, BuscaSim, Atlas Público) | Úteis; continuar apresentando |
| A Melau Pet Caetano (66.136.840/0001-94) | **Site oficial** `www.melaupetcaetano.com.br`, com CNPJ exato; referência Econodata | Correto; manter a Econodata |

Essa lógica passou a ser a definitiva:
- priorizar o site oficial;
- preservar possíveis sites e referências úteis quando não houver confirmação;
- nunca classificar diretórios como oficiais.

Os estados reais dessas 5 pesquisas viraram teste de regressão (`tests/test_web_homologacao.py` com `tests/fixtures/homologacao_revalidacao_states.json`): qualquer mudança que altere esses resultados falha nos testes.

## 2. Revisão completa e correções desta homologação

**Revisados:**
- autenticação, sessões, CSRF e origem;
- isolamento por organização e por usuário;
- proteção contra SSRF (URL, porta, DNS só público, IP fixado e redirecionamentos passo a passo);
- saldo, reserva e desconto de tokens; idempotência e concorrência;
- consultas individuais e lotes;
- revalidação;
- exportações;
- mensagens de erro;
- interface;
- configuração da Vercel.

| # | Problema encontrado | Impacto | Correção |
|---|---|---|---|
| 1 | Consulta individual pendente ficava presa "em andamento" com **1 token reservado** se a pesquisa externa fosse desligada (flag ou bloqueio da política) no meio dela | Saldo disponível reduzido até intervenção manual; tela sem saída | Com a pesquisa indisponível, a consulta é encerrada sem custo, com a reserva devolvida, ou concluída com o resultado salvo. O progresso já pago fica guardado e uma nova consulta continua dele. Vale também para itens de lote |
| 2 | Uma falha transitória do cadastro (Minha Receita fora do ar, tempo esgotado) bloqueava o CNPJ **para sempre** até um comando administrativo | CNPJ impossível de pesquisar pela interface | Nova consulta tenta o cadastro de novo, no máximo uma vez por consulta, dentro das cotas e sem custo. Falhas depois da busca paga continuam exigindo revisão |
| 3 | Limite de login por IP usava o endereço da conexão; na Vercel pode ser o do proxy | Risco de "muitas tentativas" para todos ao mesmo tempo | Na Vercel, usa o IP do cliente em `x-forwarded-for`, que a plataforma sobrescreve contra falsificação |
| 4 | Exportação XLSX falharia com caracteres de controle vindos de trechos de páginas, ou com células acima do limite do Excel | Erro 500 na exportação | Caracteres proibidos removidos e células limitadas a 32.000 caracteres |
| 5 | Histórico mostrava só o CNPJ (10 últimas) | Dificulta reabrir na demonstração | Mostra o nome da empresa e as 20 últimas |
| 6 | Item de lote reaproveitado não mostrava a classificação; verificação manual aparecia como "possível site" | Leitura confusa | Classificação exibida também no reaproveitamento; verificação manual identificada |

Nenhuma correção altera a regra de tokens, o histórico, a classificação aprovada ou o esquema do banco (sem migração).

## 3. Evidências de teste

| Bateria | Resultado |
|---|---|
| Python completo (unitários, integração, segurança, tokens, lotes, exportações, classificação) | **436 aprovados** |
| Concorrência e transações em PostgreSQL local descartável | Incluída nos 436 |
| Frontend (`node --test`) | **17 aprovados** |
| Build de produção | 17 assets, regras, entrypoint ASGI e configuração Vercel validados |
| Novos testes desta homologação | 20: resultados aprovados, pendência sem pesquisa externa, recusa da política, lote bloqueado, nova tentativa do cadastro, falhas que não se repetem, histórico, IP do cliente e XLSX robusto |

Teste funcional na interface local, com fontes simuladas e as 25 empresas salvas copiadas do Neon:

| Cenário | Resultado |
|---|---|
| Login e saldo | Saldo exibido corretamente |
| Consulta de CNPJ salvo (Melau Pet) | "Resultado histórico", 0 token, site oficial e Econodata |
| Pesquisa nova simulada | 1 token, saldo 2.469 → 2.468 |
| Mesma pesquisa repetida | Reaproveitada, 0 token |
| Lote com 15 CNPJs salvos | Concluído em menos de 8 s, 0 token, reserva devolvida |
| Exportação do lote | CSV com 15 linhas e XLSX com as abas Empresas e Links (HTTP 200) |
| Celular (375 px) | Sem rolagem horizontal; grupos do Giassi exibidos corretamente |
| Desktop | FG Prime com 4 referências e links válidos |

## 4. O que foi validado em produção e o que só em simulação

**Validado em produção (dados e chamadas reais):**
- login;
- saldo e desconto de exatamente 1 token por pesquisa concluída;
- reaproveitamento sem custo;
- pesquisa individual real e lote real de 4 CNPJs (etapa 1a);
- revalidação sem token;
- histórico e reabertura;
- exportação XLSX (conferida pelo proprietário);
- bloqueio e desbloqueio pela política;
- robots.txt e intervalo por site;
- health, rotas protegidas, cabeçalhos e logs.

**Validado só com simulação:**
- lote de **15 CNPJs novos** com pesquisa real;
- comportamento sob as cotas da etapa 1b e da etapa 2;
- as correções 1 a 6 acima (até a publicação desta branch);
- falhas reais da Minha Receita e da Serper (tempo esgotado, 5xx, 429).

## 5. Limitações conhecidas

- **Lotes dependem da página aberta.** Fechar pausa sem perder nada e reabrir retoma, mas não há processamento em segundo plano. Com 2 buscas Serper por minuto (etapa 1b), 15 CNPJs novos levam cerca de 8 a 15 min com a aba aberta.
- **Processa só a consulta que está na tela.** Abrir outra interrompe a anterior, que retoma ao ser reaberta.
- **Sites com proteção anti-robô** (ex.: `weg.net`, HTTP 403) e **páginas acima de 1 MB** não são confirmados. Ficam como possível site, com o motivo.
- **Falha durante ou depois da busca paga** (Serper) exige revisão do responsável (`retry-job`), para não gastar crédito duas vezes.
- A Minha Receita não documenta CNPJ alfanumérico.
- Logs da Vercel Hobby ficam disponíveis por cerca de 1 hora.

## 6. Riscos operacionais remanescentes

- **Plano da Vercel.** O [Hobby](https://vercel.com/docs/plans/hobby) é para uso pessoal e não comercial. O uso operacional pela empresa exige plano ou condição contratual adequada (ex.: Pro, pago) ou outra hospedagem. Nenhum plano pago foi contratado: **decisão do proprietário**.
- A confirmação de saldo da Serper vence a cada 168 h com a pesquisa ligada. Vencida, as pesquisas novas param com mensagem clara, sem custo, até a renovação.
- Um único usuário (`cristiano`) está ativo. O uso pela equipe com uma só conta esbarra no limite de 5 logins por minuto por usuário; criar contas individuais.
- O crescimento de uso depende das etapas 1b e 2 (tetos acumulados) e da conferência periódica do Billing.

## 7. Prontidão

| Uso | Status |
|---|---|
| **Apresentação à equipe** com resultados salvos | **Pronta.** Ver [`DEMONSTRACAO.md`](DEMONSTRACAO.md); sem custo e sem chamadas externas |
| **Consulta e exportação dos 25 resultados salvos** pela equipe | **Pronta** (com contas individuais) |
| **Pesquisas novas e lotes de até 15 CNPJs novos** | **Tecnicamente pronta, aguardando liberação**: Billing conferido, autorização da etapa 1b e homologação de um lote real de 15 |
| **Uso operacional contínuo pela empresa** | Depende também da decisão sobre o plano da Vercel (Hobby não comercial) |
