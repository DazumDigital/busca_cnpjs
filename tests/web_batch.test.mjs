import test from 'node:test';
import assert from 'node:assert/strict';
import {analyzeBatch,validCnpj} from '../app/web/static/core.mjs';

function cnpj(n) {
  let value=String(n).padStart(12,'0');
  for(const weights of [[5,4,3,2,9,8,7,6,5,4,3,2],[6,5,4,3,2,9,8,7,6,5,4,3,2]]) {
    const remainder=weights.reduce((sum,w,i)=>sum+w*Number(value[i]),0)%11;
    value+=remainder<2?'0':String(11-remainder);
  }return value;
}
for(const n of [1,5,10,15,16])test(`entrada de ${n} CNPJs distintos`,()=>{
  const parsed=analyzeBatch(Array.from({length:n},(_,i)=>cnpj(i+1)).join('\n'));
  assert.equal(parsed.validos,n);assert.equal(parsed.validos<=15,n<=15);
});
test('colagem com pontuação, separadores, duplicados e inválidos',()=>{
  const parsed=analyzeBatch('00.065.589/0001-06;00065589000106,123\n00055627000140');
  assert.deepEqual([parsed.identificados,parsed.validos,parsed.invalidos,parsed.duplicados],[4,2,1,1]);
  assert.equal(parsed.cnpjs[0],'00065589000106');
});
test('não truncar entrada longa nem aceitar letras ou Unicode',()=>{
  for(const invalid of ['000655890001060','a00065589000106','１２３４５６７８９０１２３４']) {
    assert.equal(validCnpj(invalid),false);assert.equal(analyzeBatch(invalid).validos,0);
  }
});
