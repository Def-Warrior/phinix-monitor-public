import test from 'node:test';
import assert from 'node:assert/strict';
import {domainName,scopeDomains,activityJobs,currentScope,additions,cycle,discordMessage,digest,Failure} from '../supabase/functions/phinix-poll/core.mjs';
const P='a21b0012-18e0-4ea3-9d8b-329bfb1f2146',OLD='b21b0012-18e0-4ea3-9d8b-329bfb1f2146',NEW='c21b0012-18e0-4ea3-9d8b-329bfb1f2146';
const asset=(endpoint,tier=4,kind=1)=>({endpoint,type:{id:kind,value:kind===7?'Wildcard':'Url'},tier:{id:tier,value:tier===5?'Out Of Scope':tier===1?'No Bounty':'Tier 1'}});
const version=(id,domains)=>({id,content:domains.map(x=>asset(x))});
const detail={id:P,domains:version(NEW,['old.example','new.example/path']),status:{id:3,value:'Open'},webLinks:{detail:'https://app.intigriti.com/programs/example/test/detail'}};
const activity={programId:P,type:{id:1},activity:{fromVersionId:OLD,toVersionId:NEW},createdAt:1700000000};
test('domain boundaries exclude credentials, addresses and malformed targets',()=>{
  for(const x of ['127.0.0.1','http://0x7f000001','http://2130706433','localhost','user:pass@example.com','ftp://example.com','foo*.example.com','example.com\\bad','example.com\n@everyone']) assert.equal(domainName(x),null,x);
  assert.equal(domainName('HTTPS://Api.Example.com/path'),'api.example.com');
  assert.equal(domainName('*.Example.com'),'*.example.com');
  assert.equal(domainName('https://bücher.example'),'xn--bcher-kva.example');
});
test('scope filtering preserves paths and excludes out of scope',()=>{
  const value=version(OLD,[]);value.content=[asset('allowed.example/path'),asset('excluded.example',5),asset('free.example',1)];
  assert.deepEqual(scopeDomains(value,OLD),{'allowed.example':['allowed.example/path'],'free.example':['free.example']});
  assert.throws(()=>scopeDomains({...value,content:null},OLD),Failure);
  assert.throws(()=>scopeDomains(value,NEW),Failure);
});
test('activity identity matches existing Python digest and rejects unavailable history',async()=>{
  const [job]=await activityJobs({records:[activity],maxCount:1});
  assert.equal(job.id,await digest(P,'scope',OLD,NEW));
  const current=currentScope(detail,P);
  const rows=await additions(job,current,scopeDomains(version(OLD,['old.example'])),scopeDomains(detail.domains));
  assert.deepEqual(rows.filter(x=>x.occurred_at>0).map(x=>x.domain),['new.example']);
  assert.equal((await activityJobs({records:[{...activity,activity:{fromVersionId:NEW,toVersionId:NEW}}],maxCount:1})).length,0);
});
test('closed programs and changed paths never create new-domain alerts',async()=>{
  const [job]=await activityJobs({records:[activity],maxCount:1});
  assert.deepEqual((await additions(job,currentScope({...detail,status:{id:4,value:'Closed'}},P),{},scopeDomains(detail.domains))),[]);
  const current={...currentScope(detail,P),domains:{'old.example':['old.example/new-path']}};
  assert.equal((await additions(job,current,{'old.example':['old.example/old-path']},current.domains)).filter(x=>x.occurred_at>0).length,0);
});
test('cycle persists the addition before external delivery and uses bounded batches',async()=>{
  const [job]=await activityJobs({records:[activity],maxCount:1});let durable=false, delivered=false;
  const payload={kind:'domain_added',domain:'new.example',endpoints:['new.example/path'],scope_url:detail.webLinks.detail,occurred_at:new Date(activity.createdAt*1000).toISOString()};
  const rpc=async(op,args)=>{
    if(op==='ingest') return [job];
    if(op==='finish_job') {assert.equal(args.additions.filter(x=>x.occurred_at>0).length,1);durable=true;return true;}
    if(op==='claim_delivery') {assert.ok(durable);return [{id:'alert',payload}];}
    if(op==='delivery_result') {assert.equal(args.code,'sent');delivered=true;return true;}
    throw new Error(op);
  };
  const fetcher=async(url,options)=>{
    if(url.startsWith('https://discord.com')) {assert.ok(durable);const message=JSON.parse(options.body);assert.equal(message.embeds[0].title,'new.example');return Response.json({id:'123'});}
    if(url.includes('/activities?')) return Response.json({records:[activity],maxCount:1});
    if(url.endsWith('/domains/'+OLD)) return Response.json({domains:version(OLD,['old.example'])});
    return Response.json(detail);
  };
  const result=await cycle({rpc,credentials:{intigriti:'fake',discord:'https://discord.com/api/webhooks/123/fake'},config:{provider_cooldown:0},owner:P,fetcher});
  assert.equal(result.sent,1);assert.ok(delivered);
  assert.equal(discordMessage(Array(8).fill({payload})).embeds.length,5);
});
test('old-version access denial does not remove a currently accessible program',async()=>{
  const [job]=await activityJobs({records:[activity],maxCount:1});let failed;
  const rpc=async(op,args)=>{if(op==='ingest')return [job];if(op==='fail_job'){failed=args;return true;}if(op==='claim_delivery')return [];throw new Error(op);};
  const fetcher=async url=>url.includes('/activities?')?Response.json({records:[activity],maxCount:1}):url.endsWith('/domains/'+OLD)?new Response('',{status:403}):Response.json(detail);
  await cycle({rpc,credentials:{intigriti:'fake'},config:{provider_cooldown:0},owner:P,fetcher});
  assert.equal(failed.unavailable,false);
});
test('provider rate limit is recorded without retrying and does not invent domains',async()=>{
  let seen;
  const rpc=async(op,args)=>{if(op==='provider_error'){seen=args;return true;}if(op==='claim_delivery')return [];throw new Error(op);};
  await cycle({rpc,credentials:{intigriti:'fake'},config:{provider_cooldown:0},owner:P,fetcher:async()=>new Response('',{status:429,headers:{'retry-after':'123'}})});
  assert.equal(seen.code,'rate_limited');assert.equal(seen.delay,123);
});
