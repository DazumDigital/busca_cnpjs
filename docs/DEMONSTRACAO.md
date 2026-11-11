# Roteiro de demonstração (10 a 15 minutos)

Apresenta a plataforma com **resultados reais já obtidos**, sem consumir tokens nem créditos da Serper. A pesquisa externa fica **desligada** durante a apresentação. Diga isso à equipe: os resultados exibidos vêm de pesquisas reais feitas antes (etapa 1a e revalidação de 10/11/2026) e de 20 registros históricos. Nada é pesquisado ao vivo.

## Antes de começar

- Abra https://busca-cnpjs.vercel.app em uma janela normal (não anônima) e entre com sua conta.
- Confira o saldo: **2.464 tokens disponíveis**.
- Deixe estes CNPJs à mão (todos já salvos; reabrir não custa nada).

## 1. Tela inicial e saldo (1 min)

- Mostre o nome do usuário no topo, o saldo e a regra "1 pesquisa nova concluída = 1 token".
- Explique que resultados já salvos são reaproveitados sem custo.

## 2. Site oficial confirmado — A Melau Pet Caetano (2 min)

- Digite **66.136.840/0001-94** e clique em **Consultar CNPJ**.
- Pontos a mostrar:
  - o rótulo **"Resultado histórico"** e a nota "nenhum token consumido";
  - **Site oficial**: `www.melaupetcaetano.com.br`, com CNPJ exato na página (abra "Ver evidências armazenadas");
  - **Referências de terceiros**: Econodata.
- Clique em **Acessar site** para abrir em outra aba.

## 3. Mesma empresa, outro estabelecimento — Apple (1 min)

- **00.623.904/0001-73**: site oficial `www.apple.com/br/`.
- Mostre a observação: confirmado pelo CNPJ de outro estabelecimento da mesma empresa (0003-35).

## 4. Filial — Giassi (2 min)

- **83.648.477/0016-91**: **Site da matriz · filial não confirmada** (`institucional.giassi.com.br`).
- Também aparecem o possível site `www.giassi.com.br`, o perfil do Facebook e a referência do Portal da Transparência.
- Explique: o site é do grupo, mas não cita esta filial; por isso não é marcado como oficial dela.

## 5. Sem confirmação, mas com links úteis (2 min)

- **83.646.984/0001-00 (Angeloni)**: **Possíveis sites** `institucional.angeloni.com.br` e `www.angeloni.com.br`, cada um com o motivo de não ter sido confirmado.
- **04.743.028/0001-70 (FG Prime)**: sem site oficial; **4 referências de terceiros** úteis (diretórios com o CNPJ).
- Mensagem central: diretório nunca vira site oficial, e um link útil não é descartado só porque a titularidade não pôde ser comprovada.

## 6. Lote de 15 empresas (3 min)

Cole no campo **Pesquisa em lote** (um por linha):

```
00.623.904/0001-73
83.646.984/0001-00
83.648.477/0016-91
04.743.028/0001-70
66.136.840/0001-94
00.055.627/0001-40
00.056.678/0001-96
00.065.589/0001-06
00.063.327/0001-02
00.064.529/0001-79
00.065.768/0001-43
00.068.581/0001-01
00.060.085/0001-01
00.061.615/0001-28
00.055.600/0001-57
```

- Mostre a contagem "Válidos: 15" e clique em **Pesquisar CNPJs**.
- Como todos estão salvos, o lote termina em segundos com **"Tokens consumidos: 0"**. Cada item mostra "resultado já salvo, sem custo" e a classificação do site.
- Clique em **Ver resultado** em um item.

## 7. Exportação e histórico (2 min)

- No lote, clique em **Exportar XLSX**: abas **Empresas** (uma linha por CNPJ) e **Links** (cada link com categoria, confiança e evidências). O CSV traz as mesmas colunas.
- Em **Últimas consultas**, mostre o nome da empresa e o botão **Reabrir consulta** (reabrir não consome tokens).

## 8. Encerramento: o que vem a seguir (1 min)

- Com a liberação da pesquisa externa, um CNPJ novo é pesquisado na hora:
  - cadastro;
  - busca;
  - validação dos sites, respeitando o robots.txt;
  - custo de 1 token, só se a pesquisa for concluída.
- Lotes de CNPJs novos avançam com a página aberta e podem ser retomados pelo histórico.
- A liberação é gradual (etapa 1b: 30 buscas), mediante autorização.

## Evite durante a apresentação

- Consultar um CNPJ **não salvo**. Com a pesquisa desligada aparece "CNPJ sem resultado armazenado. A pesquisa externa está desativada neste ambiente." Não há custo, mas interrompe o roteiro.
- Apresentar o lote ou as consultas como "pesquisa em tempo real". São resultados reais já obtidos.
