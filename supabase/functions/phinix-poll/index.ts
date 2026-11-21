import {cycle} from './core.mjs';

const url=Deno.env.get('SUPABASE_URL')!;
const service=Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!;
async function rpc(op:string,args:Record<string,unknown>) {
  const response=await fetch(url+'/rest/v1/rpc/phinix_edge_rpc',{
    method:'POST',headers:{apikey:service,Authorization:'Bearer '+service,'Content-Type':'application/json'},
    body:JSON.stringify({op,args}),signal:AbortSignal.timeout(4000),redirect:'error'
  });
  if(!response.ok) throw new Error('state_request_failed');
  return response.json();
}
Deno.serve(async(req:Request)=>{
  const key=req.headers.get('X-Phinix-Key')||'';
  const response=(body:unknown,status=200)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json','Cache-Control':'no-store'}});
  if(req.method!=='POST' || !/^[0-9a-f]{64}$/.test(key)) return response({error:'unauthorized'},401);
  const owner=crypto.randomUUID();let acquired=false;const started=Date.now();
  try {
    const mode=new URL(req.url).searchParams.get('mode');
    const cfg=await rpc(mode==='probe'?'probe':'acquire',{key,owner});
    if(!cfg.authorized) return response({error:'unauthorized'},401);
    if(!cfg.configured) return response({error:'not_configured'},503);
    if(cfg.busy) return response({status:'idle'});
    if(mode==='probe') {
      const {providerGet,activityJobs}=await import('./core.mjs');
      const body=await providerGet('/programs/activities?offset=0&limit=50',cfg.credentials.intigriti);
      const jobs=await activityJobs(body);
      return response({status:'authenticated',records:body.records.length,scope_activities:jobs.length,duration_ms:Date.now()-started});
    }
    acquired=true;
    const result=await cycle({rpc,credentials:cfg.credentials,config:cfg,owner});
    return response({status:'completed',...result});
  } catch {return response({error:'monitor_cycle_failed'},503);}
  finally {if(acquired) {try{await rpc('release',{owner,duration_ms:Date.now()-started});}catch{/* Leases expire automatically. */}}}
});
