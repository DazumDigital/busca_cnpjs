# Plataforma de Consulta de CNPJ DazumDigital

Implementação local de 06/11/2026. Consultas aos vinte registros consolidados funcionam sem pesquisas novas. Infraestrutura de produção ainda depende de configuração explícita.

## Execução no VS Code / PowerShell

Abra a pasta `C:\Cristiano\busca_cnpjs\outputs\cnpj-enrichment` no VS Code. No terminal PowerShell:

```powershell
Set-Location 'C:\Cristiano\busca_cnpjs\outputs\cnpj-enrichment'
# O ambiente da POC existente pode ser reutilizado. Não precisa ativá-lo.
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e '.[dev,web,webtest]'
if (-not (Test-Path -LiteralPath '.env.web')) {
    Copy-Item -LiteralPath '.env.web.example' -Destination '.env.web'
}
.\.venv\Scripts\python.exe -m app.web.manage init
.\.venv\Scripts\python.exe -m app.web.manage import 'data/output/consolidacao20-oficial-2026-11-04/consolidacao20.sqlite'
.\.venv\Scripts\python.exe -m app.web.manage create-user seu.usuario
```

A senha é solicitada de forma oculta e precisa ter de 12 a 256 caracteres. Não coloque senhas na linha de comando. Escolha seu próprio nome de usuário; não há senha padrão nem conta pública. O último comando deve ser executado uma vez por usuário. `init` e `import` podem ser repetidos; não reiniciam o saldo nem duplicam empresas.

Inicie o servidor:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.web.api:app --env-file .env.web --host 127.0.0.1 --port 8000
```

Abra **http://127.0.0.1:8000**. Utilize esse endereço, conforme `WEB_ORIGIN`; `localhost` é outra origem. Para parar: `Ctrl+C`. Após mudanças no backend, reinicie o servidor; após mudanças em CSS/JS, recarregue o navegador.

Os arquivos `data/**`, `.env`, `.env.poc10` e `provider_verification.json` da POC não são alterados por esses comandos. O banco web fica em `var/platform.sqlite`. Em uma cópia nova obtida pelo GitHub, os bancos privados não estão incluídos: forneça a consolidação local ao comando `import`.

## Consultas de validação sem créditos externos

| CNPJ | Resultado esperado |
| --- | --- |
| `00065589000106` | Daiju: site confirmado automaticamente e evidências institucionais |
| `00055627000140` | THP: verificação manual informada, distinta de confirmação automática |
| `00056678000196` | Softdata: candidato pendente; Website oficial vazio |
| `00068581000101` | Boschetti: referências de terceiros; FarmBy com confiança BAIXA e ressalva de município |
| `00064909000103` | Kobarg: sem site confirmado e sem referências selecionadas |

Essas consultas usam dados reais já armazenados e consomem **um token comercial por nova operação concluída**. Reabrir o histórico e recarregar o resultado não consomem outro token. Validação offline automatizada utiliza bancos descartáveis independentes do saldo da sua organização.

## API, autorização e contratos

| Rota | Regra |
| --- | --- |
| `POST /api/login` | Usuário/senha; origem exata; rate limit persistente |
| `GET /api/sessao` | Descoberta da sessão; sessão anônima retorna `autenticado=false` |
| `POST /api/logout` | Sessão + origem + token CSRF; revoga a sessão |
| `GET /api/saldo` | Sessão obrigatória; saldo compartilhado da organização |
| `POST /api/consultas` | Sessão + origem + CSRF + `Idempotency-Key`; valida CNPJ no servidor |
| `GET /api/consultas` | Últimas dez consultas da organização |
| `GET /api/consultas/{id}` | Recuperação sem cobrança e sem pesquisa |
| `GET /api/health` | `ok` somente quando o esquema persistente está disponível; 503 caso contrário |

O JSON público contém empresa, Website oficial, verificação manual, candidatos, redes sociais, referências, evidências, ressalvas, estado, identificador da consulta e saldo. Não contém credenciais, caminhos Windows, configuração do provedor ou auditoria técnica. A separação entre organizações é aplicada em cada consulta. Não há cadastro público.

Sessões opacas e aleatórias ficam em cookie HttpOnly e SameSite=Strict, com Secure em HTTPS. O banco armazena apenas o hash do token de sessão. Senhas usam PBKDF2-SHA256 com 600 mil iterações e salt individual; sessão expira em oito horas. A autenticação usa contas locais da equipe, sem integração externa obrigatória. Recuperação de senha é administrativa:

```powershell
.\.venv\Scripts\python.exe -m app.web.manage reset-password seu.usuario
```

Esse comando revoga as sessões anteriores. Não altera tokens comerciais nem cotas técnicas.

## Transações e idempotência

- A organização é criada uma única vez com 2.469 tokens. Reexecutar `init`, importar dados ou reiniciar a aplicação preserva o saldo.
- O desconto, o resultado e o lançamento no livro comercial são gravados na mesma transação. Uma restrição única impede dois lançamentos para a mesma operação.
- SQLite local usa `BEGIN IMMEDIATE`; PostgreSQL usa lock da linha da organização com `SELECT FOR UPDATE`.
- A chave de idempotência é única por organização. Repetição retorna a operação existente; mesma chave com outro CNPJ gera conflito.
- A chave pendente é lembrada no navegador apenas para recuperação de operação. O saldo vem sempre do backend; não é mantido em localStorage/JavaScript.
- CNPJ inválido, empresa ausente sem pesquisa autorizada e erro interno não consomem token. Resultado vazio concluído e nova consulta aos dados armazenados consomem um token.
- Pesquisas enfileiradas reservam um token; o token só passa a consumido quando o resultado é confirmado. Falhas liberam a reserva. O saldo disponível é inicial menos consumidos menos reservados.
- Cada usuário tem sua sessão, mas compartilha o saldo da organização. Há limitação persistente de vinte novas tentativas de consulta por minuto por organização.

## Motor existente e consultas externas

`app/web/engine.py` reutiliza `Serper`, `JsonAPI`, `QuotaManager`, `WebsiteFinder`, `WebsiteValidator` e as proteções de SSRF originais. O motor CLI não foi substituído. Não há chamada a um provedor no navegador.

**Nesta entrega, `WEB_ENABLE_EXTERNAL=false`.** A aplicação consulta os vinte registros importados sem consumir créditos externos. Nenhuma pesquisa real foi executada no desenvolvimento.

O adaptador externo foi testado com HTTP mockado. Funciona apenas em executor local explícito; não é iniciado em background pela função HTTP. Usa fila persistente no banco web. Uma operação no servidor HTTP somente cria a fila/reserva após autorização explícita do usuário e habilitação administrativa. Um executor já autorizado reivindica um único trabalho por vez.

Antes de habilitar esse executor, o responsável deve confirmar conta gratuita, ausência de cartão/cobrança automática, saldo gratuito atual e manifesto válido. Não basta haver tokens comerciais. A verificação local desta entrega encontrou o manifesto de pesquisa expirado: não foi atualizado nem liberado automaticamente.

O consumo técnico permanece em `data/poc.sqlite`, com lock compartilhado com os pilotos CLI. Os novos checkpoints ficam separados em `var/engine.sqlite`. Nunca usar uma cópia zerada como livro técnico.

Limites conservadores do adaptador: uma empresa, concorrência 1, uma pesquisa, um candidato e zero retentativas. Reutiliza `small_settings`/`pilot_policies`: teto global de websites no máximo 120, no máximo seis chamadas de websites por minuto e intervalo mínimo de cinco segundos. Por trabalho: no máximo uma reserva de pesquisa e doze de websites, sempre subordinadas aos tetos menores já configurados. Não inicia uma pesquisa quando a cota de websites acabou. CNPJs presentes no histórico não são pesquisados novamente pelo adaptador.

Após autorização específica futura, revalidação do manifesto e configuração local, o comando é:

```powershell
# Não executar enquanto WEB_ENABLE_EXTERNAL=false ou sem autorização real.
.\.venv\Scripts\python.exe -m app.web.manage work-one --authorize-external
```

O executor usa as credenciais existentes somente no servidor local, sem incluí-las no pacote Vercel. Uma interrupção abrupta deixa o trabalho `EM_PROCESSAMENTO`; ele não é reenviado automaticamente, para evitar consumo duplicado do provedor. Após parar o executor e revisar a auditoria:

```powershell
.\.venv\Scripts\python.exe -m app.web.manage cancel-job ID_DA_CONSULTA --confirmed-worker-stopped
```

Esse comando libera uma reserva comercial pendente sem zerar contadores técnicos. Não cancele um trabalho enquanto o executor ainda estiver ativo.

## Segurança e interface

O HTML usa os SVGs originais da marca e o favicon fornecido. Poppins e sua licença OFL são servidas localmente. Os tokens da interface e as cores institucionais permanecem distintos em `app/web/static/tokens.css`. O texto navy no CTA laranja preserva contraste com os tokens oficiais. Animações usam opacity/transform, a curva oficial, entrada curta, hover de cards de 350 ms e redução de movimento do sistema.

Dados retornados são inseridos com `textContent`, sem interpolação de HTML. URLs usam a validação existente de domínio/esquema e somente links HTTP/HTTPS são clicáveis. Links externos têm `noopener noreferrer`. Diretórios não viram Website oficial. Perfis sociais sem confirmação são rotulados; resultados genéricos e pessoas sem marca empresarial correspondente não são exibidos como perfis da empresa.

Há CSP restritiva, cabeçalhos de segurança, limite de 4 KB para corpos JSON, validação de origem, CSRF e SQL parametrizado. O serviço local escuta apenas em loopback. Segredos ficam no servidor. Os logs públicos de falha não incluem conteúdo bruto de exceções, parâmetros SQL ou credenciais.

## Vercel e persistência de produção

O entrypoint é `app.web.api:app`, configurado em `pyproject.toml`. Python 3.14 é especificado em `.python-version`. A Vercel detecta FastAPI a partir das dependências, promove o diretório montado `/assets` e configura uma única função Python. `vercel.json` usa o arquivo real `app/web/api.py` e um limite HTTP de 30 segundos para a modalidade de dados armazenados.

Os dados privados, bancos, ambientes virtuais, arquivos de ambiente e manifestos operacionais são excluídos por `.vercelignore` e `excludeFiles`. Não há caminhos Windows fixos no runtime web; os caminhos históricos só aparecem nos guias locais e na auditoria privada importada.

`git.deploymentEnabled=false` bloqueia deploys automáticos por push em qualquer branch. A publicação do código para revisão não autoriza nem dispara uma publicação Vercel. O fluxo manual posterior continua dependendo de autorização.

SQLite mutável é rejeitado em produção e quando `VERCEL` está definido. Persistência de produção requer PostgreSQL, por exemplo um projeto **Neon Free**, que não foi criado nesta entrega. No plano gratuito, confirme no painel os limites atuais e mantenha a conta no Free. Não habilite upgrade automático, recursos pagos ou cobrança por excedente.

Variáveis privadas por ambiente:

```dotenv
WEB_ENV=production
WEB_ORIGIN=https://SEU_DOMINIO
WEB_DATABASE_URL=postgresql+psycopg://USUARIO:SENHA@HOST/DB?sslmode=verify-full&sslrootcert=system
WEB_ENABLE_EXTERNAL=false
```

Use o hostname de pool do banco, senha codificada corretamente em URL e TLS verificado. Preview e produção devem usar bancos/branches isolados. O runtime não executa DDL, importações ou inicialização do saldo no startup/deploy. O administrador executa `init`, `import` e `create-user` uma única vez no banco selecionado, após autorização, mantendo a origem SQLite local somente leitura. A tabela de importações registra SHA-256, classificação, origem e proveniência, sem sobrescrever resultados web posteriores.

**Pendências antes de publicar:** banco persistente, credenciais do banco, usuários, domínio/origem exata e validação no ambiente Linux/Preview. A integração do executor de novas pesquisas com uma infraestrutura de produção também permanece desativada. Não há tarefas em memória tratadas como trabalhos duráveis.

**Condição de custo:** o [Hobby da Vercel](https://vercel.com/docs/plans/hobby) permite uso pessoal não comercial. A plataforma empresarial não deve ser publicada nele presumindo gratuidade comercial. Definir um plano/condição contratual apropriada ou hospedagem alternativa é necessário; nenhum plano pago foi contratado. [Neon Free](https://neon.com/docs/introduction/plans) é uma opção gratuita de banco, sujeita aos limites da conta. Isso não torna a hospedagem comercial da Vercel automaticamente gratuita.

## Testes e build

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --test tests/web_core.test.mjs
node --check app/web/static/app.js
.\.venv\Scripts\python.exe -m app.web.build
.\.venv\Scripts\python.exe -m pip wheel --no-deps --wheel-dir dist .
```

`app.web.build` verifica entrypoint, configuração, marca, fonte, favicon e dezesseis assets, sem tocar em banco nem rede. A suíte Python bloqueia DNS e conexões de rede; somente o socketpair interno da stdlib usado por asyncio no Windows é permitido. Testes usam mocks, fixtures históricas e bancos temporários. A verificação adicional PostgreSQL foi executada em cluster local isolado, sem acessar banco externo.

O wheel e as dependências com wheels Linux CPython 3.14 foram verificados. Isso não substitui a execução efetiva em Linux/Vercel. `vercel build` integral depende de projeto/ambiente vinculados e não foi apresentado como deploy validado.

Com plano, banco e projeto previamente aprovados/configurados, os passos posteriores de Preview são:

```powershell
# Apenas após configurar acesso Vercel e resolver as pendências acima.
npx vercel@65.12.1 link
npx vercel@65.12.1 pull --environment=preview
npx vercel@65.12.1 build
npx vercel@65.12.1 deploy --prebuilt
```

Esses comandos não foram executados. Nenhum deploy de produção está autorizado por este guia. Conferir o projeto, a equipe e o ambiente antes de qualquer publicação; não usar `--prod` sem nova autorização.

Documentação consultada em 06/11/2026: [FastAPI na Vercel](https://vercel.com/docs/frameworks/backend/fastapi), [runtime Python](https://vercel.com/docs/functions/runtimes/python), [plano Hobby](https://vercel.com/docs/plans/hobby) e [planos Neon](https://neon.com/docs/introduction/plans).
