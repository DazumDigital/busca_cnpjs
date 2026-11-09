export function digits(value) { return String(value).replace(/\D/g, '').slice(0, 14); }
export function mask(value) {
  const raw = digits(value);
  return raw.replace(/^(\d{2})(\d)/, '$1.$2').replace(/^(\d{2})\.(\d{3})(\d)/, '$1.$2.$3')
    .replace(/(\d{3})(\d)/, '$1/$2').replace(/(\d{4})(\d{1,2})$/, '$1-$2');
}
export function validCnpj(value) {
  if (!/^[0-9./\-\s]+$/.test(String(value))) return false;
  const raw = String(value).replace(/\D/g, '');
  if (raw.length !== 14 || /^(\d)\1{13}$/.test(raw)) return false;
  const weights = [[5,4,3,2,9,8,7,6,5,4,3,2], [6,5,4,3,2,9,8,7,6,5,4,3,2]];
  return weights.every((weight, index) => {
    const remainder = weight.reduce((sum, n, position) => sum + n * Number(raw[position]), 0) % 11;
    return Number(raw[12 + index]) === (remainder < 2 ? 0 : 11 - remainder);
  });
}

export function analyzeBatch(value) {
  const entries=String(value).split(/[\n,;]+/).map(s=>s.trim()).filter(Boolean);
  const cnpjs=[], invalid=[]; let duplicates=0;
  for(const entry of entries) {
    const cnpj=entry.replace(/[^0-9]/g,'');
    if(!/^[0-9./\-\s]+$/.test(entry) || !validCnpj(cnpj)) invalid.push(entry);
    else if(cnpjs.includes(cnpj)) duplicates++;
    else cnpjs.push(cnpj);
  }
  return {identificados:entries.length,validos:cnpjs.length,invalidos:invalid.length,duplicados:duplicates,cnpjs};
}
export function safeLink(value) {
  try {
    const url = new URL(value);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.port || !url.hostname.includes('.')) return '';
    if (/[\\\x00-\x20]/.test(value)) return '';
    return url.href;
  } catch { return ''; }
}
export function groups(result) {
  return [
    {key:'oficial', title:'Site oficial', items:result.website_oficial ? [result.website_oficial] : []},
    {key:'manual', title:'Verificação manual informada', items:result.verificacao_manual ? [result.verificacao_manual] : []},
    {key:'possiveis', title:'Possíveis sites · para revisão', items:result.possiveis_sites || []},
    {key:'sociais', title:'Redes sociais · vínculo não confirmado', items:result.redes_sociais || []},
    {key:'referencias', title:'Referências de terceiros', items:result.referencias || []},
  ];
}
