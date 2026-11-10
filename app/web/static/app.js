import {digits, mask, validCnpj, safeLink, groups, analyzeBatch, failureMessage, needsProcessing, nextBatchItem, waitMessage, tokenNote} from './core.mjs';

const $ = id => document.getElementById(id);
const number = new Intl.NumberFormat('pt-BR');
const state = {csrf:'', busy:false, external:false, result:null, filter:'todos', balance:null, before:{}, poll:null, batch:null, batchRunning:false, batchLoopActive:false};
const types = {BUSINESS_DIRECTORY:'Diretório empresarial', INSTITUTIONAL_THIRD_PARTY_DOCUMENT:'Documento institucional de terceiros',
  THIRD_PARTY_REFERENCE:'Página de referência', REFERENCE_PAGE:'Página de referência', SOCIAL_NETWORK:'Rede social',
  CONFIRMADO_AUTOMATICAMENTE:'Titularidade confirmada', VERIFICACAO_MANUAL_INFORMADA:'Verificação manual informada',
  POSSIVEL_SITE:'Possível site', REDE_SOCIAL:'Perfil social', SITE_DA_MATRIZ:'Site da matriz'};
Object.assign(types,{PAGINA_DE_REFERENCIA:'Página de referência',DIRETORIO_EMPRESARIAL:'Diretório empresarial',DOCUMENTO_INSTITUCIONAL_TERCEIRO:'Documento institucional de terceiros'});
const confidence = {ALTA:'Alta confiança', MEDIA:'Confiança média', BAIXA:'Baixa confiança', PENDENTE:'Pendente', MANUAL:'Informada', NAO_CONFIRMADO:'Não confirmado'};
const processing = {CONCLUIDA:'Concluída', NA_FILA:'Aguardando pesquisa', EM_PROCESSAMENTO:'Em andamento', FALHOU:'Não concluída'};
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function memoryGet(key) { try { return JSON.parse(sessionStorage.getItem(key) || 'null'); } catch { return null; } }
function memorySet(key, value) { try { value === null ? sessionStorage.removeItem(key) : sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* Idempotency still protected in-memory for this page. */ } }
let pending = memoryGet('dazum_pending');
async function api(path, options={}) {
  const response = await fetch(path, {credentials:'same-origin', cache:'no-store', ...options,
    headers:{'Content-Type':'application/json', ...(state.csrf ? {'X-CSRF-Token':state.csrf} : {}), ...options.headers}});
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== '/api/login') signedOut();
    const error = new Error(data.mensagem || 'Não foi possível concluir a consulta. Tente novamente.');
    error.code = data.erro; error.status = response.status; throw error;
  }
  return data;
}
function signedOut() {
  state.csrf=''; $('login-panel').hidden=false; $('search-panel').hidden=true; $('logout').hidden=true;
  $('current-user').hidden=true; $('current-user').textContent='';
  $('historico').hidden=true; $('results').hidden=true; $('progress-panel').hidden=true;
  $('balance').textContent='—'; $('consumed').textContent='—'; state.balance=null;
  state.batchRunning=false; state.batch=null; $('batch-panel').hidden=true;
  if(state.poll) clearTimeout(state.poll);
}
function updateBalance(balance) {
  const changed=state.balance!==null && state.balance!==balance.disponiveis;
  state.balance=balance.disponiveis;
  $('balance').textContent=number.format(balance.disponiveis); $('consumed').textContent=number.format(balance.consumidos);
  $('reservation').hidden=balance.reservados===0;
  if(changed) { const wallet=$('balance').closest('.wallet'); wallet.classList.remove('pulse'); requestAnimationFrame(()=>wallet.classList.add('pulse')); }
  $('search-button').disabled=state.busy || state.batchLoopActive || balance.disponiveis<=0;
  $('search-help').textContent=`Saldo: ${number.format(balance.disponiveis)} tokens. Pesquisa nova concluída usa 1 token; resultado já salvo é reaproveitado sem custo.`;
  updateBatchInput();
  if(balance.disponiveis<=0 && !$('search-error').textContent) $('search-error').textContent='Você não possui tokens suficientes para realizar uma nova consulta.';
}
async function refreshBalance() { if(state.csrf) updateBalance(await api('/api/saldo')); }
async function authorized() {
  const session=await api('/api/sessao');
  if(!session.autenticado) {signedOut();return;}
  state.csrf=session.csrf; state.external=session.pesquisa_externa_disponivel;
  $('current-user').textContent=session.usuario; $('current-user').title='Conectado como '+session.usuario; $('current-user').hidden=false;
  $('batch-mode').textContent=state.external?'Resultados existentes são reaproveitados. Empresas novas são pesquisadas progressivamente, respeitando os limites aprovados. 1 token por pesquisa nova concluída; resultados já salvos são reaproveitados sem custo.':'Consulte os dados já pesquisados. Novas pesquisas externas estão desativadas. CNPJs sem registro não são cobrados.';
  $('login-panel').hidden=true; $('search-panel').hidden=false; $('logout').hidden=false; $('historico').hidden=false;
  await refreshBalance(); await history(); await batchHistory(); await restoreBatch();
  if(pending && validCnpj(pending.cnpj)) { $('cnpj').value=mask(pending.cnpj); await submit(pending.external); }
  else { const last=memoryGet('dazum_last'); if(last) await reopen(last, false); }
}
function busy(on) {
  state.busy=on; $('search-button').disabled=on || state.batchLoopActive || state.balance<=0; $('cnpj').disabled=on;
  updateBatchInput();
  $('search-button').querySelector('.spinner').hidden=!on; $('search-button-label').textContent=on?'Consultando…':'Consultar CNPJ';
  $('search-form').setAttribute('aria-busy', String(on));
}
$('login-form').addEventListener('submit', async event=>{
  event.preventDefault(); $('login-button').disabled=true; $('login-error').textContent='';
  try { await api('/api/login',{method:'POST',body:JSON.stringify({usuario:$('username').value,senha:$('password').value})}); $('password').value=''; await authorized(); $('cnpj').focus(); }
  catch(error) { $('login-error').textContent=error.message; }
  finally { $('login-button').disabled=false; }
});
$('logout').addEventListener('click', async()=>{
  try { await api('/api/logout',{method:'POST'}); memorySet('dazum_last',null); memorySet('dazum_pending',null); pending=null; signedOut(); }
  catch(error) { toast(error.message); }
});
$('cnpj').addEventListener('input', event=>{
  // Keep editing in the middle of a masked number predictable.
  const input=event.target, before=input.value, cursor=input.selectionStart;
  const count=before.slice(0,cursor).replace(/\D/g,'').length;
  input.value=mask(before);
  let index=0, seen=0; while(index<input.value.length && seen<count) { if(/\d/.test(input.value[index])) seen++; index++; }
  input.setSelectionRange(index,index);
  const complete=digits(input.value).length===14, valid=validCnpj(input.value);
  $('input-check').textContent=valid?'✓':''; input.setAttribute('aria-invalid', String(complete && !valid));
  $('search-error').textContent=complete && !valid?'O CNPJ informado é inválido. Confira os números e tente novamente.':'';
  $('external-option').hidden=true;
});
$('search-form').addEventListener('submit', event=>{event.preventDefault(); submit(false);});
$('external-button').addEventListener('click',()=>submit(true));
async function submit(external=false) {
  if(state.busy || state.batchLoopActive) return;
  const cnpj=digits($('cnpj').value);
  if(!validCnpj(cnpj)) { $('search-error').textContent='O CNPJ informado é inválido. Confira os números e tente novamente.'; $('cnpj').setAttribute('aria-invalid','true'); $('cnpj').focus(); return; }
  if(!pending || pending.cnpj!==cnpj) pending={cnpj,key:crypto.randomUUID(),external,before:state.balance};
  // A retry uses the same operation even if the prior response was lost.
  memorySet('dazum_pending',pending); busy(true); $('search-error').textContent=''; $('external-option').hidden=true; $('results').hidden=true;
  try {
    const data=await api('/api/consultas',{method:'POST',headers:{'Idempotency-Key':pending.key},body:JSON.stringify({cnpj,autorizar_pesquisa_externa:pending.external})});
    if(typeof pending.before==='number') state.before[data.id]=pending.before;
    memorySet('dazum_last',data.id); pending=null; memorySet('dazum_pending',null);
    await showConsultation(data); await history();
  } catch(error) {
    $('search-error').textContent=error.message;
    // Configuration and quota refusals are final for this operation: no automatic resubmission.
    const final=error.status && (error.status<500 || ['INTEGRACAO_NAO_VERIFICADA','PESQUISA_SUSPENSA','COTA_EXTERNA_ESGOTADA','CONFIGURACAO_PENDENTE'].includes(error.code));
    if(final) { pending=null; memorySet('dazum_pending',null); }
    if(error.code==='EMPRESA_NAO_PESQUISADA' && state.external) $('external-option').hidden=false;
    if(!final) $('search-error').textContent+=' Envie novamente para recuperar a mesma operação, sem desconto duplicado.';
    try { await refreshBalance(); } catch { /* The next recovery will refresh the authoritative balance. */ }
  } finally { busy(false); }
}
async function showConsultation(data, focus=true) {
  // A reopened consultation replaces any message left by a previous one.
  $('search-error').textContent='';
  updateBalance(data.saldo);
  const running=needsProcessing(data,state.external);
  $('progress-panel').hidden=!running;
  if(state.poll) clearTimeout(state.poll);
  if(running) {
    $('results').hidden=true;
    $('progress-panel').querySelector('p').textContent=data.retry_after?waitMessage(data.retry_after):data.status==='CONCLUIDA'?'Revalidando os sites já encontrados, sem nova busca paga e sem novo token.':({CADASTRO:'Identificando a empresa na base cadastral.',BUSCA:'Pesquisando websites e referências.',WEBSITE:'Validando os candidatos com segurança.'})[data.etapa_pesquisa] || 'Pesquisa registrada. O progresso permanece salvo.';
    if(state.external)state.poll=setTimeout(async()=>{
      try{const next=await api('/api/consultas/'+encodeURIComponent(data.id)+'/processar',{method:'POST'});await showConsultation(next,false);await history();}
      catch(error){$('search-error').textContent=error.message+' Reabra a consulta pelo histórico para retomar.';}
    },Math.max(1500,(data.retry_after || 0)*1000));
    return;
  }
  if(data.status==='FALHOU') { $('search-error').textContent=failureMessage(data.erro); if(!data.resultado){$('results').hidden=true;return;} }
  if(data.resultado) { state.result=data.resultado; state.filter='todos'; render(data); if(focus) { $('results').focus({preventScroll:true}); $('results').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'}); } }
}
async function reopen(id, focus=true) {
  try { const data=await api('/api/consultas/'+encodeURIComponent(id)); memorySet('dazum_last',id); await showConsultation(data,focus); }
  catch(error) { if(error.status===404 && !focus) {memorySet('dazum_last',null);return;} $('search-error').textContent=error.message; }
}
function render(data) {
  exportButtons($('individual-exports'),'/api/consultas/'+encodeURIComponent(data.id)+'/exportar');
  const result=state.result, company=result.empresa; $('results').hidden=false;
  $('company-card').replaceChildren();
  const monogram=node('div','company-monogram',(company.nome_fantasia || company.razao_social || 'E').trim()[0].toUpperCase());
  const details=node('div','company-details'); details.append(node('h3','',company.razao_social || 'Empresa pesquisada'));
  if(company.nome_fantasia) details.append(node('p','',company.nome_fantasia));
  const meta=node('div','company-meta'); meta.append(node('span','',mask(company.cnpj)));
  if(company.municipio || company.uf) meta.append(node('span','',[company.municipio,company.uf].filter(Boolean).join(' · ')));
  if(company.situacao_cadastral) meta.append(node('span','',company.situacao_cadastral));
  details.append(meta); $('company-card').append(monogram,details,node('span','company-status','Pesquisa registrada'));
  const all=groups(result), total=all.reduce((sum,g)=>sum+g.items.length,0);
  $('result-date').textContent=(result.origem_resultado==='PESQUISA_REAL'?'Pesquisa real registrada · ':'Resultado histórico · ')+new Date(data.criado_em*1000).toLocaleString('pt-BR',{dateStyle:'short',timeStyle:'short'});
  $('result-summary').textContent=(total?`${total} ${total===1?'link identificado':'links identificados'} · Fontes organizadas por tipo de vínculo.`:'Nenhum link encontrado. Não identificamos sites ou referências para este CNPJ.')+' '+tokenNote(data,state.before?.[data.id]);
  $('filters').replaceChildren();
  for(const filter of [{key:'todos',title:'Todos',count:total},...all.filter(g=>g.items.length).map(g=>({key:g.key,title:({oficial:'Oficial',manual:'Manual',matriz:'Site da matriz',possiveis:'Possíveis sites',google:'Painel do Google',sociais:'Redes sociais',referencias:'Referências'})[g.key],count:g.items.length}))]) {
    const button=node('button','filter'+(state.filter===filter.key?' active':''),filter.title); button.type='button'; button.setAttribute('aria-pressed',String(state.filter===filter.key)); button.append(node('small','',filter.count));
    button.addEventListener('click',()=>{state.filter=filter.key; render(data); $('filters').querySelector('[aria-pressed="true"]').focus({preventScroll:true});}); $('filters').append(button);
  }
  $('result-groups').replaceChildren();
  if(!result.website_oficial && state.filter==='todos') {
    const title=result.verificacao_manual?'Sem confirmação automática de site oficial':result.site_matriz?.length?'Site da matriz encontrado · filial não confirmada':result.status==='FALHA_VALIDACAO_SITE_OFICIAL'?'Validação técnica pendente':(result.possiveis_sites.length || result.indicado_google?.length)?'Há um candidato para revisão':'Sem site oficial confirmado';
    const empty=node('div','empty-state'); empty.append(node('h3','',title),node('p','',result.verificacao_manual?'Há uma verificação manual informada, apresentada separadamente abaixo.':'Os links encontrados permanecem classificados conforme as evidências disponíveis.'));
    $('result-groups').append(empty);
  }
  for(const group of all) {
    if(!group.items.length || (state.filter!=='todos' && state.filter!==group.key)) continue;
    const section=node('section','result-group'), title=node('h3','group-title',group.title); title.append(node('span','',group.items.length));
    const grid=node('div','links-grid'); group.items.forEach(item=>{ const card=renderLink(item,group.key); if(card) grid.append(card); }); section.append(title,grid); $('result-groups').append(section);
  }
  $('data-note').textContent=result.ressalvas.join(' ')+(company.pesquisado_em? ' Pesquisa de origem: '+new Date(company.pesquisado_em*1000).toLocaleDateString('pt-BR')+'.':'');
  for(const source of result.fontes || [])$('data-note').append(node('span','',` Fonte: ${source.nome}.`));
}
function textEvidence(value) { return Array.isArray(value)?value.join(' · '):String(value || ''); }
function renderLink(item,key) {
  const url=safeLink(item.url); if(!url) return null;
  const card=node('article','link-card'+(key==='oficial'?' official':key==='manual'?' manual':''));
  const top=node('div','card-top'); top.append(node('span','source-type',types[item.tipo] || 'Fonte de referência'),node('span','badge'+(item.confianca==='BAIXA'?' low':item.confianca==='ALTA'?' high':''),confidence[item.confianca] || 'Para revisão'));
  card.append(top,node('h3','',item.dominio || new URL(url).hostname),node('p','full-url',url),node('p','evidence',textEvidence(item.evidencia)));
  if(item.indicado_google===true) card.append(node('p','google-note','Indicado pelo painel do Google'));
  for(const alert of item.ressalvas || []) card.append(node('p','alert',alert));
  if(item.revisao_necessaria && !(item.ressalvas || []).length) card.append(node('p','data-note','Correspondência parcial. Referência disponível para revisão.'));
  if(item.paginas?.length) { const disclosure=node('details','evidence'); disclosure.append(node('summary','','Ver evidências armazenadas')); for(const page of item.paginas) {disclosure.append(node('p','',page.titulo || page.url)); if(page.trecho) disclosure.append(node('p','',page.trecho));} card.append(disclosure); }
  const actions=node('div','link-actions'), open=node('a','open-link',['oficial','possiveis','matriz','google'].includes(key)?'Acessar site':key==='sociais'?'Abrir perfil':'Abrir referência'); open.href=url; open.target='_blank'; open.rel='noopener noreferrer'; open.append(node('span','','↗'));
  const copy=node('button','copy','Copiar link'); copy.type='button'; copy.setAttribute('aria-label','Copiar link de '+item.dominio);
  copy.addEventListener('click',async()=>{try { await navigator.clipboard.writeText(url); toast('Link copiado'); }catch { toast('Selecione o link e copie pelo navegador.'); }});
  actions.append(open,copy); card.append(actions); return card;
}
async function history() {
  const entries=await api('/api/consultas'); $('history-list').replaceChildren();
  if(!entries.length) $('history-list').append(node('p','data-note','Suas consultas aparecerão aqui.'));
  for(const entry of entries) {
    const row=node('div','history-row'), info=node('div',''); info.append(node('span','history-cnpj',mask(entry.cnpj)),node('p','',new Date(entry.criado_em*1000).toLocaleString('pt-BR')+' · '+(processing[entry.status] || 'Registrada')));
    const open=node('button','','Reabrir consulta ↗'); open.addEventListener('click',()=>reopen(entry.id)); row.append(info,open); $('history-list').append(row);
  }
}
$('new-query').addEventListener('click',()=>{$('cnpj').value=''; $('input-check').textContent=''; $('search-error').textContent=''; $('cnpj').setAttribute('aria-invalid','false'); $('cnpj').focus(); $('consulta').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});});
let toastTimer;
function toast(message) { clearTimeout(toastTimer); $('toast').textContent=message; $('toast').hidden=false; toastTimer=setTimeout(()=>$('toast').hidden=true,3000); }

function updateBatchInput() {
  const parsed=analyzeBatch($('batch-input').value);
  $('batch-count').textContent=`Identificados: ${parsed.identificados} · Válidos: ${parsed.validos} · Inválidos: ${parsed.invalidos} · Duplicados: ${parsed.duplicados}`;
  const insufficient=state.balance!==null && parsed.validos>state.balance;
  $('batch-error').textContent=parsed.validos>15?'Informe no máximo 15 CNPJs válidos e distintos.':insufficient?`Este lote exige até ${parsed.validos} tokens. Seu saldo disponível é ${number.format(state.balance)}.`:parsed.invalidos || parsed.duplicados?'Entradas inválidas e duplicadas serão ignoradas, sem cobrança.':'';
  $('batch-button').disabled=!state.csrf || state.busy || state.batchLoopActive || parsed.validos<1 || parsed.validos>15 || insufficient;
  $('batch-input').disabled=state.batchLoopActive;
}
function exportButtons(container,path) {
  container.replaceChildren();
  for(const format of ['csv','xlsx']) {
    const button=node('button','outline','Exportar '+format.toUpperCase()); button.type='button';
    button.addEventListener('click',async()=>{
      button.disabled=true;
      try {
        const response=await fetch(path+'?formato='+format,{credentials:'same-origin',cache:'no-store'});
        if(!response.ok) {const error=await response.json();throw new Error(error.mensagem || 'Não foi possível exportar.');}
        const url=URL.createObjectURL(await response.blob());const anchor=node('a');anchor.href=url;anchor.download='busca-cnpjs.'+format;
        document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
      }catch(error){toast(error.message);}finally{button.disabled=false;}
    });container.append(button);
  }
}
function showBatch(data) {
  state.batch=data;memorySet('dazum_batch_last',data.id);$('batch-panel').hidden=false;updateBalance(data.saldo);
  const p=data.progresso;
  $('batch-note').textContent=data.retry_after?waitMessage(data.retry_after)+' Os demais itens continuam enquanto isso.':'O progresso fica salvo. Você pode sair e retomar os itens pendentes pelo histórico.';
  $('batch-state').textContent=({PENDENTE:'Pendente',EM_ANDAMENTO:'Em andamento',CONCLUIDO:'Concluído',CONCLUIDO_COM_ERROS:'Concluído com pendências'})[data.status] || data.status;
  $('batch-progress').textContent=`Total: ${p.total} · Concluídos: ${p.concluidos} · Em andamento: ${p.em_andamento} · Pendentes: ${p.pendentes} · Erros: ${p.erros} · Tokens consumidos: ${p.tokens_consumidos}`;
  $('batch-progress-bar').max=p.total;$('batch-progress-bar').value=p.concluidos+p.erros;
  $('batch-resume').hidden=!p.pendentes || state.batchLoopActive;$('batch-pause').hidden=!state.batchRunning;$('batch-cancel').hidden=!p.pendentes;
  $('batch-items').replaceChildren();
  for(const item of data.itens) {
    const name=item.resultado?.empresa?.nome_fantasia || item.resultado?.empresa?.razao_social;
    const found=item.resultado || {};
    const site=found.website_oficial?'Site oficial confirmado':found.site_matriz?.length?'Site da matriz (filial não confirmada)':(found.possiveis_sites?.length || found.indicado_google?.length)?'Possível site para revisão':'Sem site oficial confirmado';
    const row=node('div','history-row'), info=node('div');info.append(node('span','history-cnpj',mask(item.cnpj)+(name?' · '+name:'')),node('p','',item.erro?failureMessage(item.erro):item.status==='CONCLUIDA'?(item.token_consumido===false?'Concluída · resultado já salvo, reaproveitado · sem custo':'Concluída · 1 token · '+site):({PENDENTE_LOTE:'Pendente',EM_PROCESSAMENTO:'Em andamento',FALHOU:'Não concluída'})[item.status] || item.status));
    row.append(info);if(item.resultado){const button=node('button','','Ver resultado ↗');button.type='button';button.addEventListener('click',()=>reopen(item.id));row.append(button);}$('batch-items').append(row);
  }
  exportButtons($('batch-exports'),'/api/lotes/'+encodeURIComponent(data.id)+'/exportar');
}
async function openBatch(id,resume=false) {
  if(state.batchLoopActive){$('batch-process-error').textContent='Aguarde a conclusão do item atual antes de abrir outro lote.';return;}
  try {const data=await api('/api/lotes/'+encodeURIComponent(id));showBatch(data);if(resume) await runBatch();}
  catch(error){$('batch-process-error').textContent=error.message;if(error.status===404)memorySet('dazum_batch_last',null);}
}
async function runBatch() {
  if(state.batchLoopActive || !state.batch || !state.csrf) return;
  state.batchLoopActive=true;state.batchRunning=true;memorySet('dazum_batch_run',true);$('batch-process-error').textContent='';showBatch(state.batch);
  const notBefore=new Map();
  try {
    while(state.batchRunning && state.csrf) {
      const batch=state.batch,next=nextBatchItem(batch.itens,notBefore,Date.now());
      if(!next.item) {
        if(!next.wait)break;
        // Every pending item is waiting: sleep in short slices so Pause stays responsive.
        await new Promise(resolve=>setTimeout(resolve,Math.min(1000,next.wait)));continue;
      }
      const data=await api(`/api/lotes/${encodeURIComponent(batch.id)}/itens/${encodeURIComponent(next.item.id)}/processar`,{method:'POST'});
      if(!state.csrf)break;showBatch(data);
      if(data.retry_after)notBefore.set(next.item.id,Date.now()+data.retry_after*1000);
    }
  }catch(error){$('batch-process-error').textContent=error.message+' Use Retomar para recuperar os itens pendentes, sem desconto duplicado.';}
  finally {
    state.batchLoopActive=false;state.batchRunning=false;memorySet('dazum_batch_run',false);if(state.csrf && state.batch){showBatch(state.batch);await batchHistory().catch(()=>{});await history().catch(()=>{});}
  }
}
async function batchHistory() {
  const entries=await api('/api/lotes');$('batch-history-list').replaceChildren();
  if(!entries.length)$('batch-history-list').append(node('p','data-note','Seus lotes aparecerão aqui.'));
  for(const entry of entries){const row=node('div','history-row'),info=node('div');info.append(node('span','history-cnpj',`${entry.progresso.total} empresas · ${entry.progresso.concluidos} concluídas`),node('p','',new Date(entry.criado_em*1000).toLocaleString('pt-BR')+` · ${entry.progresso.pendentes} pendentes`));const button=node('button','','Abrir lote ↗');button.type='button';button.addEventListener('click',()=>openBatch(entry.id));row.append(info,button);$('batch-history-list').append(row);}
}
async function restoreBatch() {
  const draft=memoryGet('dazum_batch_pending');if(draft){$('batch-input').value=draft.value;await createBatch();return;}
  const id=memoryGet('dazum_batch_last');if(id)await openBatch(id,memoryGet('dazum_batch_run')===true);
}
async function createBatch() {
  if(state.batchLoopActive || state.busy)return;
  const value=$('batch-input').value,parsed=analyzeBatch(value);
  if(!parsed.validos || parsed.validos>15){updateBatchInput();return;}
  let draft=memoryGet('dazum_batch_pending');if(!draft || draft.value!==value)draft={value,key:crypto.randomUUID()};
  memorySet('dazum_batch_pending',draft);busy(true);$('batch-error').textContent='';
  try {
    const data=await api('/api/lotes',{method:'POST',headers:{'Idempotency-Key':draft.key},body:JSON.stringify({cnpjs:value})});
    memorySet('dazum_batch_pending',null);showBatch(data);busy(false);await runBatch();
  }catch(error){$('batch-error').textContent=error.message;if(error.status && error.status<500)memorySet('dazum_batch_pending',null);}
  finally{busy(false);}
}
$('batch-input').addEventListener('input',updateBatchInput);
$('batch-form').addEventListener('submit',event=>{event.preventDefault();createBatch();});
$('batch-resume').addEventListener('click',()=>runBatch());
$('batch-pause').addEventListener('click',()=>{state.batchRunning=false;memorySet('dazum_batch_run',false);if(state.batch)showBatch(state.batch);});
$('batch-cancel').addEventListener('click',async()=>{
  if(!state.batch)return;state.batchRunning=false;memorySet('dazum_batch_run',false);
  try{const data=await api('/api/lotes/'+encodeURIComponent(state.batch.id)+'/cancelar',{method:'POST'});showBatch(data);await batchHistory();}
  catch(error){$('batch-process-error').textContent=error.message;}
});
window.addEventListener('scroll',()=>$('header').classList.toggle('scrolled',scrollY>8),{passive:true});
if('IntersectionObserver' in window && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
  const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){entry.target.classList.add('visible');observer.unobserve(entry.target);}}),{threshold:.12}); document.querySelectorAll('.reveal').forEach(element=>observer.observe(element));
}
setInterval(()=>{if(!document.hidden && state.csrf && !state.busy) refreshBalance().catch(()=>{});},15000);
// Enable native submission only after every form handler has been registered.
$('login-button').disabled=false;
authorized().catch(()=>signedOut());
