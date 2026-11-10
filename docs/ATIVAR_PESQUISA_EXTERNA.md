# Ativar a pesquisa externa no Busca CNPJs

O piloto usa **uma conta isolada, um CNPJ válido e inédito e até uma chamada à Serper**. Os tokens de teste pertencem à organização de QA; não use o saldo da equipe. Em caso de falha ou execução incerta, não repita a chamada sem revisar a auditoria e obter nova autorização.

## Antes de habilitar

1. No painel da [Serper](https://serper.dev/), confirme a conta que emitirá a chave, o saldo exclusivamente gratuito e a data/hora UTC da conferência (validade de até 24 horas no app). Confirme ausência de cartão ou outro meio de cobrança, créditos pagos, recarga automática e cobrança por excedente. Confirme o limite aplicável à conta e as condições de uso automatizado e armazenamento de links. A oferta pública de créditos não comprova a situação da conta.
2. Obtenha autorização explícita para configurar a chave, habilitar a pesquisa **somente para o piloto** e executar uma pesquisa para o CNPJ informado. Verifique que ele não esteja no histórico da equipe nem no cache de pesquisas anteriores.
3. Publique a correção de empacotamento a partir da revisão aprovada no Git, com a pesquisa desligada. Verifique o deployment de Git na Vercel: `/api/health` deve responder 200 no domínio autorizado e `/api/lotes` deve responder 401 sem sessão. Preserve as variáveis de banco, ambiente e origem já existentes. A configuração continua sem deploy automático por push.

## Configuração, somente após autorização

- Em **Production** do projeto **dazum-digital/busca-cnpjs**, mantenha `WEB_ENV=production`, `WEB_ORIGIN=https://busca-cnpjs.vercel.app` e o `WEB_DATABASE_URL` existente. Use `SERPER_API_KEY` como **Secret**, com a chave da conta verificada. Não envie o valor por chat, em argumentos de comandos ou no Git. Mantenha `WEB_ENABLE_EXTERNAL=false` enquanto prepara o banco.
- Crie ou use uma organização de teste isolada, com uma conta ativa e um token de teste. Prepare um manifesto privado fora do Git, com `allowed_organizations` contendo só o ID de QA e `allowed_cnpjs` contendo só os 14 dígitos do CNPJ aprovado. Para cada provedor (`serper`, `minhareceita`, `websites`), informe `max_requests`, `per_minute`, `per_hour`, `per_day` e, opcionalmente, `min_interval` em segundos. Os quatro tetos são inteiros positivos; o teto total considera todos os registros técnicos anteriores e não reinicia ao publicar um novo manifesto.
- No piloto, use **no máximo uma nova chamada Serper e uma cadastral**: para cada um, defina o teto total como o consumo técnico correspondente já registrado no Neon mais um e os tetos por minuto/hora/dia em 1. Para websites, use no máximo oito novas reservas (consumo técnico anterior mais oito), com `per_minute=6`, `per_hour=8`, `per_day=8` e `min_interval=10`; reduza se a regra do site exigir. A [Minha Receita](https://docs.minhareceita.org/) fornece o cadastro, sem chave; as páginas são inspecionadas separadamente, respeitando robots.txt e validação de URLs.
- Na seção `serper`, registre `confirmed_free_remaining` como o saldo realmente confirmado, `balance_verified_at` como data/hora ISO 8601 em UTC, `reviewed_on` e `expires_on` como datas ISO, e `evidence` com a referência da conferência, sem segredos. Somente após verificar cada condição, confirme `verified`, `free_only`, `no_card`, `no_auto_charge`, `no_paid_credits`, `storage_permitted` e `batch_permitted` como booleanos verdadeiros. Se a evidência faltar, mantenha a pesquisa desligada. O limite local não substitui a ausência de cobrança na conta.
- Use um arquivo privado de ambiente com o URL Neon já configurado, a **mesma** chave Serper do Secret da Vercel e `WEB_ENABLE_EXTERNAL=false`. Com o terminal na raiz do projeto e o ambiente virtual ativo, execute:

```powershell
python -m app.web.manage --env-file 'CAMINHO_PRIVADO_DO_ENV' configure-external 'CAMINHO_PRIVADO_DO_MANIFESTO'
```

O comando registra a política e vincula sua aprovação à chave sem gravar o valor da chave no Neon. Não altera empresas nem tokens comerciais, não limpa o livro técnico e não chama provedores. Se já existir consumo fora do Neon, use o saldo recém-verificado da conta e avalie se o livro legado precisa ser importado com `--legacy-ledger`; não substitua o histórico por um contador zerado.

Só depois disso, defina `WEB_ENABLE_EXTERNAL=true` em Production e faça um novo deployment da revisão validada para aplicar as variáveis. Entre na conta isolada e execute **somente** a pesquisa autorizada. Confirme os dados e fontes na interface, a persistência no Neon, até uma reserva da Serper e o saldo intacto da equipe. Compare o saldo do provedor no painel e volte a desligar a flag após o piloto. A abertura para a equipe ou a autorização de novos CNPJs exige nova decisão de escopo e limites.

## Comandos administrativos (todos locais, com o arquivo privado de ambiente)

Use `$py = 'C:\Cristiano\busca_cnpjs\outputs\cnpj-enrichment\.venv\Scripts\python.exe'` e `$envfile` com o caminho do arquivo privado. Nenhum destes comandos chama provedores externos, imprime segredos ou altera o saldo de organizações existentes.

| Objetivo | Comando |
|---|---|
| Criar organização isolada de QA com 1 token (não altera organização existente) | `& $py -m app.web.manage --env-file $envfile create-org qa-serper-weg --tokens 1` |
| Criar usuário na organização de QA (senha pedida interativamente) | `& $py -m app.web.manage --env-file $envfile create-user qa.piloto.weg --org qa-serper-weg` |
| Registrar/renovar a política (chave lida do ambiente, só o hash é salvo) | `& $py -m app.web.manage --env-file $envfile configure-external MANIFESTO.json` |
| Ver política (sem hash da chave), livro técnico e estado das pesquisas | `& $py -m app.web.manage --env-file $envfile external-status` |
| **Desligar imediatamente pelo banco**, sem redeploy | `& $py -m app.web.manage --env-file $envfile block-external` |
| Saldo de uma organização | `& $py -m app.web.manage --env-file $envfile balance --org qa-serper-weg` |
| Liberar pesquisa falha/incerta após revisar a auditoria | `& $py -m app.web.manage --env-file $envfile retry-job 84429695000111 --confirmed-reviewed` |
| Desativar a conta de teste preservando consultas e auditoria | `& $py -m app.web.manage --env-file $envfile deactivate-user qa.piloto.weg` |
| Reativar uma conta desativada (organização e saldo inalterados) | `& $py -m app.web.manage --env-file $envfile activate-user qa.piloto.weg` |
| Refazer **só a validação de sites** de uma pesquisa concluída, com os candidatos já obtidos (sem Serper e sem cadastro; exige autorização) | `& $py -m app.web.manage --env-file $envfile revalidate-sites 84429695000111 --confirmed-authorized` |

`block-external` grava `blocked: true` na política: nenhuma etapa externa nova é reservada, mesmo com `WEB_ENABLE_EXTERNAL=true`. Para voltar, é preciso rodar `configure-external` com um manifesto revisado. `retry-job` não apaga o livro técnico; a próxima etapa usa um novo identificador de operação e continua contando nas cotas.

## Como a pesquisa real funciona

Cada clique ou passo do lote executa **uma** etapa HTTP com no máximo uma chamada externa:

1. **Cadastro** (Minha Receita, sem chave): razão social, nome fantasia, município, UF e situação cadastral. CNPJ ausente na base → `CNPJ_NAO_ENCONTRADO`, sem token.
2. **Busca** (Serper, 1 crédito): nome + município + "site oficial", excluindo diretórios conhecidos. Somente as URLs são guardadas, nunca a SERP integral.
3. **Validação de sites** (até 3 sites candidatos e 8 acessos por consulta, robots.txt respeitado, intervalo mínimo de 10 s): o site é oficial apenas com CNPJ exato, nome, titularidade em rodapé/página legal, marca no título e domínio compatível. Se o primeiro candidato falhar ou for de terceiro, o próximo é tentado.

O `robots.txt` segue a RFC 9309: 200 é interpretado; 4xx (exceto 429) significa que não há regras e o acesso segue; 5xx encerra o candidato. O status HTTP de cada falha fica registrado na pesquisa e aparece no motivo exibido (ex.: "(HTTP 403)"). Proteções anti-robô de CDNs costumam bloquear IPs de datacenter como os da Vercel; nesses casos o candidato fica pendente, com o motivo, e nenhuma técnica de evasão é usada. Só domínios ou subdomínios com a marca da empresa aparecem como "Possíveis sites" quando não puderam ser validados; páginas de terceiros sem a marca não.

Falhas de um site candidato (bloqueio por robots, site fora do ar, limite do site, redirecionamentos) **não derrubam a consulta**: o resultado é concluído e salvo com o status `FALHA_VALIDACAO_SITE_OFICIAL` e o candidato aparece em "Possíveis sites" com o motivo. Se a cota total de websites do manifesto acabar no meio da validação, a consulta também é concluída localmente com o que já foi obtido, sem nova chamada.

### Descoberta de sites (melhorias de 10/11/2026, após a etapa 1a)

- **Busca**: nomes limpos (sem artigo inicial, "& CIA", "LTDA", "S/A"), nome fantasia **ou** razão social e a localidade, sem "site oficial". Ex.: `"MELAU PET" OR "MELAU PET CAETANO" JOINVILLE SC`. Uma busca por CNPJ; a segunda busca (nome completo + "contato") só existe se a política tiver `"max_searches_per_cnpj": 2` e a primeira não trouxer domínio da marca — decisão de custo, desligada por padrão. O token continua 1 por pesquisa.
- **Ordem de validação**: site do painel do Google, domínios e subdomínios da marca, **raiz do domínio** de um subdomínio da marca (ex.: `institucional.angeloni.com.br` → `www.angeloni.com.br`, se existir no DNS) e, se a busca não trouxe nenhum domínio da marca, até 2 **domínios deduzidos do nome** que existam no DNS (ex.: `melaupetcaetano.com.br`). Só sem nenhum desses é validado **um** candidato sem a marca.
- **Plataformas de terceiros** (mapas, vagas, jurídico, diretórios, notícias, associações, portais públicos) não gastam acessos nem aparecem como possível site; podem continuar como referência.
- **Mesma raiz de CNPJ**: para matriz consultada, uma página com outro estabelecimento da mesma raiz (mesma pessoa jurídica) é reavaliada com as mesmas evidências fortes e pode ser confirmada como oficial, com a observação. Para filial, continua a categoria "Site da matriz".
- **Domínio formado pelo nome** (inclusive sigla curta, ex.: `fgprime.com.br`) conta como domínio da marca; a confirmação exige CNPJ, nome, titularidade e marca no título.
- **Exibição**: raiz do site (não a página interna), perfil social (não o post), motivo da falha com o código HTTP ou a categoria da conexão (tempo esgotado, certificado TLS, conexão recusada, DNS). Domínio deduzido que falhou não aparece.

### Regra de tokens (aprovada em 10/11/2026; substitui a de 09/11)

- **1 token** por **pesquisa real concluída**: cadastro e busca responderam e o resultado foi salvo, com ou sem site oficial confirmado e mesmo sem links.
- **Sem custo**: CNPJ inválido; pesquisa bloqueada antes de executar (política, cota, limite); falha técnica que impeça a conclusão (cadastro ou busca); CNPJ ausente na base cadastral; reabrir ou recarregar uma consulta; revalidar sites (`revalidate-sites`).
- **CNPJ já pesquisado**: o resultado salvo é **reaproveitado sem custo**, com aviso "Resultado já salvo, reaproveitado: nenhum token consumido". Não há nova cobrança sem pesquisa nova. Isso vale também para os 20 registros históricos e para itens de lote (a reserva feita na criação do lote é devolvida).
- **Atomicidade**: o débito acontece na mesma transação que conclui a consulta; o livro de tokens tem a consulta como chave, então cliques repetidos, concorrência, falha e retomada nunca duplicam o desconto. Cada débito fica registrado com data, organização e consulta.
- A tela mostra o saldo antes da pesquisa e, ao concluir, "1 token consumido nesta pesquisa · saldo X → Y".

### Classificação exibida

- **Site oficial confirmado**: CNPJ exato, nome, titularidade em rodapé/página legal, marca no título e domínio da marca.
- **Site da matriz (filial)**: para CNPJ de filial, site no domínio da marca que mostra só inscrições da mesma raiz, incluindo a da matriz (0001). Indica o site do grupo; **não confirma a filial**.
- **Possível site**: domínio ou subdomínio com a marca, ainda não confirmado; mostra o motivo quando a validação falhou (ex.: "(HTTP 403)"). Bloqueios de validação não escondem o possível site.
- **Indicado pelo painel do Google**: grupo próprio para o site informado pelo painel do Google na busca; nunca vira oficial por isso. Só a URL é guardada.
- **Referência de terceiro**: diretórios e páginas com o CNPJ exato ou nome e município.
- **Rede social**: perfil com a marca no endereço, vínculo não confirmado.

As exportações CSV/XLSX trazem duas colunas novas no fim ("Site da matriz (filial)" e "Indicado pelo painel do Google"), sem deslocar as existentes.

### Robô e mensagens

O robô se identifica como `DazumCNPJ/1.0 (+https://busca-cnpjs.vercel.app/robo)`; a página pública `/robo` explica o que ele acessa e como bloqueá-lo (`User-agent: DazumCNPJ` / `Disallow: /`). O `robots.txt` é lido nos primeiros 500 KiB, de qualquer tipo de conteúdo; linhas inválidas, longas ou HTML são ignoradas e só as regras para `*` ou `DazumCNPJ` ficam no estado.

| Código | Quando | Mensagem para a equipe |
|---|---|---|
| `PESQUISA_NAO_LIBERADA` (403) | Organização ou CNPJ fora da política | Conta não liberada para pesquisas externas deste CNPJ |
| `PESQUISA_SUSPENSA` (503) | Política bloqueada (`block-external` ou recusa da Serper) | Pesquisa suspensa pelo responsável |
| `INTEGRACAO_NAO_VERIFICADA` (503) | Sem política, chave diferente ou confirmação de saldo vencida | Aguarda configuração ou renovação da confirmação |
| `LIMITE_CONSULTAS` (429) | Mais de 20 consultas ou lotes por minuto na organização | Limite de consultas por minuto; nada cobrado |
| `COTA_EXTERNA_ESGOTADA` | Teto total aprovado atingido | Limite de pesquisas externas atingido |

Esperas de 2 minutos ou mais (janelas por hora ou dia) aparecem como "Limite de pesquisas do período atingido", com o tempo estimado; o progresso fica salvo.

Limite temporário (HTTP 429) do cadastro ou da busca pausa a etapa até o cooldown (mínimo 60 s), no máximo 3 vezes; depois disso a pesquisa falha sem desconto.

## Liberação gradual para a equipe (aprovada em 09/11/2026)

Os tetos `max_requests` são **acumulados** com o livro técnico do Neon. Consumo já registrado (piloto WEG): Serper 1, Minha Receita 1, sites 8. Confira com `external-status` e some a margem aprovada. `balance_valid_hours` aceita até 720 h quando a conta Serper não tem meio de pagamento; o consumo desde `balance_verified_at` nunca passa de `confirmed_free_remaining`. Cada etapa exige nova conferência do Billing da Serper e autorização no chat.

| Etapa | Escopo | Serper (novas) | Minha Receita (novas) | Sites (novos) |
|---|---|---|---|---|
| 1a — validação | `dazumdigital`, só os 5 CNPJs confirmados pelo proprietário (`allowed_cnpjs`) | limites da etapa 1; na prática até 5 (uma busca por CNPJ) | limites da etapa 1; na prática até 5 | limites da etapa 1; até 8 por CNPJ (40) |
| 1b — uso inicial | `dazumdigital`, qualquer CNPJ válido | 30 no total · 2/min · 20/h · 30/dia | 60 · 4/min · 40/h · 60/dia | 240 · 12/min · 200/h · 240/dia |
| 2 — teto aprovado | `dazumdigital`, qualquer CNPJ, após homologar um lote real de até 15 | 500 · 5/min · 60/h · 150/dia | 1.000 · 10/min · 120/h · 300/dia | 4.000 · 30/min · 600/h · 2.000/dia |

Manifesto da etapa 1b (valores acumulados com o piloto: 1 + 30, 1 + 60, 8 + 240). Na etapa 1a, acrescente `"allowed_cnpjs"` com os 5 CNPJs, sem pontuação.

```json
{
  "allowed_organizations": ["dazumdigital"],
  "serper": {"max_requests": 31, "per_minute": 2, "per_hour": 20, "per_day": 30,
             "confirmed_free_remaining": 2467, "balance_verified_at": "AAAA-MM-DDTHH:MM:SS+00:00",
             "balance_valid_hours": 168, "reviewed_on": "AAAA-MM-DD", "expires_on": "AAAA-MM-DD",
             "evidence": "Billing conferido pelo proprietário: saldo, sem cartão, sem pagamentos, auto top-up desabilitado",
             "verified": true, "free_only": true, "no_card": true, "no_auto_charge": true,
             "no_paid_credits": true, "storage_permitted": true, "batch_permitted": true},
  "minhareceita": {"max_requests": 61, "per_minute": 4, "per_hour": 40, "per_day": 60},
  "websites": {"max_requests": 248, "per_minute": 12, "per_hour": 200, "per_day": 240, "min_interval": 0}
}
```

Nos sites, `min_interval: 0` evita uma fila global; o intervalo de pelo menos 10 s (ou o `Crawl-delay`) continua valendo **por site**. Os lotes avançam em rodízio: com 2 buscas Serper por minuto, um lote de 15 CNPJs novos leva cerca de 8 minutos com a página aberta; com 5 por minuto (etapa 2), cerca de 3 a 4 minutos.

## Processamento de lotes: como funciona e limites

- A fila é **persistente no Neon** (consultas, itens de lote e pesquisas). Cada etapa externa é uma requisição curta, com no máximo uma chamada a uma fonte.
- **Quem aciona as etapas é a página aberta**: os itens avançam em rodízio (um item esperando não trava os outros). Fechar a página **pausa** sem perder nada; reabrir o lote pelo histórico retoma do ponto em que parou, sem cobrar de novo.
- **Não há processamento em segundo plano** hoje.
- Limites da Vercel no plano Hobby (documentação oficial conferida em 10/11/2026): cron jobs no máximo **uma vez por dia**; "Schedules" no máximo **a cada 15 minutos**, com variação de até 15 minutos; funções até 300 s (o projeto usa 30 s).
- Opções para rodar sem a aba aberta, **a decidir**:
  - **A (sem custo):** um agendamento da Vercel a cada 15 minutos chama um endpoint protegido que avança os lotes pendentes por alguns minutos. Um lote de 15 terminaria em cerca de 15 a 45 minutos. Exige implementação, testes e aprovação.
  - **B (pago):** plano Pro, com cron por minuto. Exige aprovação financeira.

## Cotas e retomada

O PostgreSQL serializa a reserva das etapas externas. A cota total, o saldo confirmado e as janelas móveis de minuto, hora e dia incluem reservas incertas e chamadas que falharam. O bloqueio da Serper por falta de créditos ou acesso negado impede novas chamadas até revisão. Uma espera de limite temporário mantém o progresso salvo; cota da Serper ou do cadastro definitivamente esgotada encerra o item sem desconto comercial e a pesquisa retoma da mesma fase quando houver nova cota. Repetir a mesma operação ou etapa não duplica o desconto. Um lote aceita até 15 CNPJs distintos e progride por etapas; recarregar a página recupera o lote pelo histórico.
