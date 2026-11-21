// No provider credentials or domain data are logged by this worker.
export const API = 'https://api.intigriti.com/external/researcher/v1';
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const uuid = value => {
  if (typeof value !== 'string' || !UUID.test(value)) throw new Failure('schema_error');
  return value.toLowerCase();
};
export class Failure extends Error {
  constructor(code, delay = 60, status = 0) { super(code); this.code = code; this.delay = delay; this.status = status; }
}
export async function digest(...parts) {
  const bytes = new TextEncoder().encode(JSON.stringify(parts));
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(x=>x.toString(16).padStart(2,'0')).join('');
}
export function domainName(value) {
  if (typeof value !== 'string' || value.trim().length > 1800 || !value.trim()) return null;
  value = value.trim();
  if (/[\x00-\x20\\]/.test(value)) return null;
  try {
    const u = new URL(value.includes('://') ? value : 'https://' + value);
    if (!['http:','https:'].includes(u.protocol) || u.username || u.password) return null;
    const host = u.hostname.toLowerCase().replace(/\.$/, '');
    const wildcard = host.startsWith('*.');
    const bare = wildcard ? host.slice(2) : host;
    if (bare.length > 253 || !bare.includes('.') || /^\d+(\.\d+){3}$/.test(bare)) return null;
    if (!bare.split('.').every(x=>/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(x))) return null;
    return (wildcard ? '*.' : '') + bare;
  } catch { return null; }
}
export function scopeDomains(version, expected) {
  if (!version || uuid(version.id) !== (expected || uuid(version.id)) || !Array.isArray(version.content) || version.content.length>10000) throw new Failure('scope_content_unavailable');
  const result = {};
  for (const item of version.content) {
    if (!item?.type || !item?.tier || typeof item.endpoint !== 'string' || typeof item.tier.value !== 'string') throw new Failure('schema_error');
    const tier = item.tier.value.toLowerCase();
    if (item.tier.id===5 || tier==='out of scope' || (tier!=='no bounty' && !/^tier [1-5]$/.test(tier))) continue;
    if (!(item.type.id===1 && item.type.value==='Url') && !(item.type.id===7 && item.type.value==='Wildcard')) continue;
    const name=domainName(item.endpoint);
    if (name) (result[name] ||= []).push(item.endpoint);
  }
  return Object.fromEntries(Object.entries(result).map(([k,v])=>[k,[...new Set(v)].sort()]));
}
export function programUrl(value) {
  const u = new URL(value);
  if (u.protocol!=='https:' || !['app.intigriti.com','intigriti.com'].includes(u.hostname) || u.username || u.password) throw new Failure('schema_error');
  if (u.search) {
    const redirect=u.searchParams.get('redirect');
    if (u.hostname!=='app.intigriti.com' || [...u.searchParams.keys()].some(x=>x!=='redirect') || !redirect?.startsWith('/') || redirect.startsWith('//') || redirect.includes('\\')) throw new Failure('schema_error');
    return new URL(redirect,u.origin).href;
  }
  return u.href;
}
export function currentScope(detail, programId) {
  if (uuid(detail?.id)!==programId) throw new Failure('schema_error');
  const domains=scopeDomains(detail.domains);
  const opened=detail.status?.id===3 && detail.status?.value==='Open';
  return {domains:opened?domains:{},scope_url:programUrl(detail.webLinks?.detail),status:opened?'open':'not_open'};
}
export async function activityJobs(body) {
  if (!Array.isArray(body?.records) || body.records.length>50 || !Number.isInteger(body.maxCount) || body.maxCount<body.records.length) throw new Failure('schema_error');
  const jobs=[];
  for (const record of body.records) {
    if (![1,3].includes(record?.type?.id)) continue;
    const program_id=uuid(record.programId), occurred_at=record.createdAt;
    if (!Number.isSafeInteger(occurred_at) || occurred_at<0 || occurred_at>253402300799) throw new Failure('schema_error');
    const kind=record.type.id===1?'scope':'status';
    let from_version=null,to_version=null;
    if(kind==='scope') {
      from_version=uuid(record.activity?.fromVersionId); to_version=uuid(record.activity?.toVersionId);
      if(from_version==='00000000-0000-0000-0000-000000000000' || from_version===to_version) continue;
    }
    const id=kind==='scope'?await digest(program_id,kind,from_version,to_version):await digest(program_id,kind,occurred_at);
    jobs.push({id,program_id,kind,from_version,to_version,occurred_at});
  }
  return jobs;
}
export async function additions(job,current,before,after) {
  const rows=[];
  for(const [domain,endpoints] of Object.entries(current.domains)) {
    rows.push({id:await digest('baseline',job.program_id,domain),change_id:'baseline:'+job.program_id,domain,endpoints,occurred_at:0});
    if (Object.hasOwn(after,domain) && !Object.hasOwn(before,domain)) rows.push({id:await digest(job.id,domain),change_id:job.id,domain,endpoints,occurred_at:job.occurred_at});
  }
  return rows;
}
export function retryDelay(response,body) {
  const raw=response.headers.get('retry-after');
  const seconds=Number(raw ?? body?.retry_after);
  if (Number.isFinite(seconds) && seconds>0) return Math.max(1,Math.min(seconds,86400));
  const date=Date.parse(raw||'');
  return Number.isFinite(date)?Math.max(1,Math.min((date-Date.now())/1000,86400)):60;
}
export async function readJson(response,max=2_000_000) {
  const reader=response.body?.getReader(); if(!reader) throw new Failure('schema_error');
  const chunks=[];let size=0;
  for (;;) {const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>max){await reader.cancel();throw new Failure('response_too_large');}chunks.push(value);}
  const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
  try{return JSON.parse(new TextDecoder().decode(bytes));}catch{throw new Failure('schema_error');}
}
export async function providerGet(path,token,fetcher=fetch) {
  if(!/^\/(?:programs\/activities\?offset=0&limit=50|programs\/[0-9a-f-]{36}(?:\/domains\/[0-9a-f-]{36})?)$/.test(path)) throw new Failure('invalid_resource');
  let response;
  try {response=await fetcher(API+path,{headers:{Authorization:'Bearer '+token,Accept:'application/json'},redirect:'error',signal:AbortSignal.timeout(2200)});}catch{throw new Failure('upstream_unavailable',15);}
  if(response.status===429) throw new Failure('rate_limited',retryDelay(response),429);
  if(response.status===401) throw new Failure('auth_error',3600,401);
  if([403,404].includes(response.status)) throw new Failure('scope_access_denied',3600,response.status);
  if(!response.ok) throw new Failure('upstream_unavailable',30,response.status);
  return readJson(response);
}
const safe = value => String(value).replaceAll('@','@\u200b').replace(/[\x00-\x1f]/g,' ').replaceAll('`','ˋ');
export function discordMessage(rows) {
  return {username:'Phinix',allowed_mentions:{parse:[],users:[],roles:[],replied_user:false},embeds:rows.slice(0,5).map(row=>({
    title:safe(row.payload.domain).slice(0,240),url:programUrl(row.payload.scope_url),color:5763719,
    description:('New in-scope domain\n'+row.payload.endpoints.map(x=>'`'+safe(x)+'`').join('\n')).slice(0,650)+'\nVerify the complete scope using the link above.',
    timestamp:new Date(row.payload.occurred_at).toISOString(),footer:{text:'Phinix · Intigriti domain added'}
  }))};
}

// Dependencies are injected so scope decisions and delivery can be tested offline.
export async function cycle({rpc,credentials,config,owner,fetcher=fetch,now=()=>Date.now()}) {
  const started=now(); let checked=0,sent=0;
  const call=(op,args={})=>rpc(op,{...args,owner});
  if(config.provider_cooldown*1000<=now()) {
    try {
      const body=await providerGet('/programs/activities?offset=0&limit=50',credentials.intigriti,fetcher);
      const jobs=await activityJobs(body),hash=await digest(jobs.map(x=>x.id));
      const pending=await call('ingest',{jobs,hash});
      for(let start=0;start<pending.length && now()-started<14000;start+=2) {
        const results=await Promise.allSettled(pending.slice(start,start+2).map(async job=>{
          let current;
          try {
            // Fetch the current and previous versions concurrently on a scope change.
            const [detail,old]=await Promise.all([
              providerGet('/programs/'+job.program_id,credentials.intigriti,fetcher),
              job.kind==='scope'?providerGet('/programs/'+job.program_id+'/domains/'+job.from_version,credentials.intigriti,fetcher).then(data=>({data}),error=>({error})):Promise.resolve(null)
            ]);
            current=currentScope(detail,job.program_id);let before={},after={};
            if(job.kind==='scope' && Object.keys(current.domains).length) {
              if(old.error) throw old.error;
              before=scopeDomains(old.data.domains,job.from_version);
              const next=uuid(detail.domains.id)===job.to_version?detail:await providerGet('/programs/'+job.program_id+'/domains/'+job.to_version,credentials.intigriti,fetcher);
              after=scopeDomains(next.domains,job.to_version);
            }
            await call('finish_job',{job_id:job.id,...current,additions:await additions(job,current,before,after)}); checked++;
          } catch(e) {
            const err=e instanceof Failure?e:new Failure('schema_error');
            await call('fail_job',{job_id:job.id,code:err.code,delay:err.delay,unavailable:!current&&[403,404].includes(err.status)});
            if(['rate_limited','auth_error'].includes(err.code)) throw err;
          }
        }));
        const failed=results.find(x=>x.status==='rejected');
        if(failed) throw failed.reason;
      }
    } catch(e) {
      const err=e instanceof Failure?e:new Failure('poll_failed');
      await call('provider_error',{code:err.code,delay:err.delay});
    }
  }
  // One bounded Discord request per cycle; at most five domain embeds per request.
  if(now()-started<20000) {
    const rows=await call('claim_delivery');
    if(rows.length) {
      const ids=rows.map(x=>x.id);
      let result={ids,code:'network_error',delay:30};
      try {
        if(!/^https:\/\/discord\.com\/api(?:\/v10)?\/webhooks\/[0-9]+\/[A-Za-z0-9_-]+$/.test(credentials.discord)) throw new Failure('invalid_webhook');
        const response=await fetcher(credentials.discord+'?wait=true',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(discordMessage(rows)),redirect:'error',signal:AbortSignal.timeout(3000)});
        const body=await readJson(response,100_000).catch(()=>({}));
        if(response.ok && typeof body.id==='string') {result={ids,code:'sent',message_id:body.id};sent=rows.length;}
        else if(response.status===429) result={ids,code:'rate_limited',delay:retryDelay(response,body)};
        else result={ids,code:'webhook_rejected',delay:30,permanent:response.status>=400&&response.status<500&&response.status!==408};
      } catch(e) { if(e instanceof Failure && e.code==='invalid_webhook') result={ids,code:'invalid_webhook',permanent:true}; }
      await call('delivery_result',result);
    }
  }
  return {checked,sent,duration_ms:now()-started};
}
