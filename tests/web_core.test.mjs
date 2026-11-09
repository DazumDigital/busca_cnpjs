import {test} from 'node:test';
import assert from 'node:assert/strict';
import {validCnpj, mask, safeLink, groups, failureMessage, needsProcessing, nextBatchItem} from '../app/web/static/core.mjs';

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
    assert.equal(failureMessage(code),'Falha técnica ao consultar as fontes externas. Nenhum token foi consumido.');
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
  const result=groups({website_oficial:null,verificacao_manual:{url:'https://thpbrasil.com.br/'},possiveis_sites:[{url:'https://softdata.com.br/'}],referencias:[{url:'https://app.farmby.com.br/'}]});
  assert.equal(result[0].items.length,0);assert.equal(result[1].items.length,1);assert.equal(result[2].items.length,1);assert.equal(result[4].items.length,1);
});
