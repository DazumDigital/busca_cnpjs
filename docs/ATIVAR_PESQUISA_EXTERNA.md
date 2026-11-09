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

## Cotas e retomada

O PostgreSQL serializa a reserva das etapas externas. A cota total, o saldo confirmado e as janelas móveis de minuto, hora e dia incluem reservas incertas e chamadas que falharam. O bloqueio da Serper por falta de créditos ou acesso negado impede novas chamadas até revisão. Uma espera de limite temporário mantém o progresso salvo; cota definitivamente esgotada encerra o item sem desconto comercial. Repetir a mesma operação ou etapa não duplica o desconto. Um lote aceita até 15 CNPJs distintos e progride por etapas; recarregar a página recupera o lote pelo histórico.
