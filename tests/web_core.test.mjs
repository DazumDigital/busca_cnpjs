import {test} from 'node:test';
import assert from 'node:assert/strict';
import {validCnpj, mask, safeLink, groups, failureMessage, needsProcessing, nextBatchItem, waitMessage, tokenNote} from '../app/web/static/core.mjs';

test('saldo antes e depois: pesquisa nova custa 1 token; reaproveitar não custa',()=>{
  const done={status:'CONCLUIDA',token_consumido:true,saldo:{disponiveis:2468}};
  assert.equal(tokenNote(done,2469),'1 token consumido nesta pesquisa · saldo 2.469 → 2.468.');
  assert.equal(tokenNote(done,undefined),'1 token consumido nesta pesquisa.');
  assert.match(tokenNote({status:'CONCLUIDA',token_consumido:false,saldo:{disponiveis:2469}},2469),/reaproveitado: nenhum token consumido · saldo 2\.469/);
  assert.equal(tokenNote({status:'NA_FILA',saldo:{disponiveis:2469}},2469),'');
});

test('esperas longas indicam limite do período; curtas, intervalo entre acessos',()=>{
  assert.match(waitMessage(12),/intervalo seguro entre acessos: próxima verificação em 12s/);
  assert.match(waitMessage(3600),/Limite de pesquisas do período atingido.*cerca de 60 min/);
  assert.match(waitMessage(undefined),/1s/);
});
test('conta não liberada, suspensão, configuração e limite por minuto têm mensagens próprias',()=>{
  const codes=['PESQUISA_NAO_LIBERADA','PESQUISA_SUSPENSA','INTEGRACAO_NAO_VERIFICADA','LIMITE_CONSULTAS','COTA_EXTERNA_ESGOTADA'];
  assert.equal(new Set(codes.map(failureMessage)).size,codes.length);
  assert.match(failureMessage('PESQUISA_NAO_LIBERADA'),/não está liberada/);
  assert.match(failureMessage('LIMITE_CONSULTAS'),/por minuto/);
});

test('consulta concluída só volta a processar com revalidação pendente e pesquisa liberada',()=>{
  assert.equal(needsProcessing({status:'NA_FILA'},false),true);
  assert.equal(needsProcessing({status:'CONCLUIDA',revalidacao_pendente:true},true),true);
  assert.equal(needsProcessing({status:'CONCLUIDA',revalidacao_pendente:true},false),false);
  assert.equal(needsProcessing({status:'CONCLUIDA',revalidacao_pendente:false},true),false);
  assert.equal(needsProcessing({status:'FALHOU',revalidacao_pendente:true},true),false);
});
test('lote em rodízio: item em espera não bloqueia os demais',()=>{
  const items=[{id:'a',status:'EM_PROCESSAMENTO'},{id:'b',status:'PENDENTE_LOTE'},{id:'c',status:'CONCLUIDA'}];
  const wait=new Map([['a',5000]]);
  assert.equal(nextBatchItem(items,wait,1000).item.id,'b');
  assert.deepEqual(nextBatchItem(items,new Map([['a',5000],['b',3000]]),1000),{item:null,wait:2000});
  assert.equal(nextBatchItem(items,wait,6000).item.id,'a');
  assert.deepEqual(nextBatchItem([{id:'c',status:'CONCLUIDA'},{id:'d',status:'FALHOU'}],wait,0),{item:null,wait:0});
});

test('falhas exibem a causa operacional sem ecoar códigos desconhecidos',()=>{
  assert.match(failureMessage('CNPJ_NAO_ENCONTRADO'),/não identificada/);
  assert.match(failureMessage('COTA_EXTERNA_ESGOTADA'),/Limite de pesquisas externas/);
  assert.match(failureMessage('LIMITE_TEMPORARIO'),/temporariamente/);
  assert.match(failureMessage('ETAPA_INTERROMPIDA'),/não foi repetida/);
  for(const code of ['FALHA_TECNICA_PESQUISA','https://x.example/?key=1','__proto__','constructor',undefined]) {
    assert.equal(failureMessage(code),'Falha técnica ao consultar as fontes externas. Nenhum token foi consumido. Tente novamente mais tarde; se persistir, fale com o responsável.');
  }
  for(const code of ['CNPJ_NAO_ENCONTRADO','COTA_ESGOTADA','ACESSO_NEGADO','INTEGRACAO_NAO_VERIFICADA']) assert.match(failureMessage(code),/Nenhum token/);
});

test('máscara completa e incremental, preservando zeros',()=>{
  assert.equal(mask('00065589000106'),'00.065.589/0001-06');
  assert.equal(mask('00.065.589/0001-06'),'00.065.589/0001-06');
  for(let i=1;i<=14;i++) assert.equal(mask('00065589000106'.slice(0,i)).replace(/\D/g,''),'00065589000106'.slice(0,i));
});
test('dígitos verificadores corretos e rejeição de inválidos',()=>{
  for(const cnpj of ['00065589000106','00055627000140','00056678000196','00068581000101']) assert.equal(validCnpj(cnpj),true);
  for(const cnpj of ['00000000000000','123','00065589000107']) assert.equal(validCnpj(cnpj),false);
});
test('links ativos permitem apenas HTTP/HTTPS',()=>{
  for(const url of ['javascript:alert(1)','data:text/html,<svg>','https://user:pass@example.com/','https://example.com:9000/']) assert.equal(safeLink(url),'');
  assert.equal(safeLink('https://thpbrasil.com.br/'),'https://thpbrasil.com.br/');
});
test('manual, candidato e diretórios nunca entram no grupo oficial',()=>{
  const result=groups({website_oficial:null,verificacao_manual:{url:'https://thpbrasil.com.br/'},possiveis_sites:[{url:'https://softdata.com.br/'}],referencias:[{url:'https://app.farmby.com.br/'}],
    site_matriz:[{url:'https://matriz.com.br/'}],indicado_google:[{url:'https://painel.com.br/'}]});
  const by=Object.fromEntries(result.map(g=>[g.key,g.items.length]));
  assert.deepEqual(by,{oficial:0,manual:1,matriz:1,possiveis:1,google:1,sociais:0,referencias:1});
  assert.deepEqual(result.map(g=>g.key),['oficial','manual','matriz','possiveis','google','sociais','referencias']);
});
