'use strict';
const $ = id => document.getElementById(id);
let offset = 0, total = 0, active = false, loading = false, domainOnly = false;
let lastUpdate = '';
const pageSize = 25;
function text(tag, value, cls) { const e = document.createElement(tag); e.textContent = value; if (cls) e.className = cls; return e; }
function badge(value) { const good = ['healthy','sent','ready','enabled'].includes(value); const bad = ['error','auth_error','failed'].includes(value); const e = text('span', value.replaceAll('_',' '), 'badge' + (good ? '' : bad ? ' error' : ['syncing','degraded','rate_limited','pending','sending'].includes(value) ? ' warning' : ' neutral')); return e; }
function date(value) { return value ? new Date(value).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : 'Not yet'; }
function notice(message='') { $('notice').textContent = message; $('notice').classList.toggle('hidden', !message); }
async function api(path, options={}) { const r = await fetch(path, {credentials:'same-origin', ...options, headers:{'Content-Type':'application/json', ...options.headers}}); const data = await r.json(); if (!r.ok) { const err = new Error(typeof data.detail === 'string' ? data.detail : 'Request failed'); err.status = r.status; throw err; } return data; }
function signedIn(value) { active = value; $('login').classList.toggle('hidden', value); $('workspace').classList.toggle('hidden', !value); $('logout').classList.toggle('hidden', !value); $('refresh').classList.toggle('hidden', !value); if (!value) { $('connection').textContent='Waiting for sign-in'; $('connection').className='badge neutral'; } }
function renderStatus(s) {
  const edge = s.mode === 'supabase_edge';
  const cloud = edge || s.mode === 'github_snapshot';
  domainOnly = s.focus === 'intigriti_domains';
  if(domainOnly) renderDomainFocus();
  $('event-count').textContent=(domainOnly?s.domain_monitoring.active_domains:s.event_count).toLocaleString();
  const enabled=s.integrations.filter(p=>p.enabled).length;
  $('provider-count').replaceChildren(document.createTextNode(String(enabled)),text('span',domainOnly?' / 1':' / 3'));
  $('provider-summary').textContent=edge ? 'Intigriti checked every 6 seconds' : cloud ? 'Monitoring runs hourly on GitHub' : enabled ? 'Collections run independently' : 'Waiting for configuration';
  const counts=domainOnly?{...s.notifications.counts,sent:s.domain_monitoring.sent_alerts,pending:s.domain_monitoring.pending_alerts,sending:0}:s.notifications.counts;
  $('sent-count').textContent=counts.sent.toLocaleString(); $('pending-count').textContent=(counts.pending+counts.sending).toLocaleString();
  $('failed-summary').textContent=counts.failed ? `${counts.failed} failed — review delivery below` : 'No failed deliveries';
  $('poll-label').textContent=edge ? '6-second monitoring · Supabase' : cloud ? 'Hourly monitoring · GitHub' : `Poll interval · ${s.poll_interval_seconds}s`;
  const names={hackerone:'HackerOne',bugcrowd:'Bugcrowd',intigriti:'Intigriti'}, icons={hackerone:'h1',bugcrowd:'◉',intigriti:'⌁'};
  $('provider-cards').replaceChildren(...s.integrations.filter(p=>!domainOnly || p.name==='intigriti').map(p=>{const card=text('article','','provider-card'); const top=text('div','','provider-card-top'); const name=text('div','','provider-name'); name.append(text('span',icons[p.name],'provider-icon'),text('span',names[p.name]));top.append(name,badge(p.status));card.append(top,text('p',p.reason || (p.status==='healthy' ? 'All configured collections are up to date.' : p.status==='auth_error' ? 'Check credentials and API permissions.' : p.status==='rate_limited' ? 'Waiting for the provider cooldown.' : 'Review collection progress below.')));return card;}));
  const current=badge(s.ready?'ready':'degraded'); $('connection').className=current.className; $('connection').textContent=cloud ? (s.snapshot_refresh_error ? 'Snapshot refresh failed' : s.snapshot_stale ? 'Monitoring snapshot is stale' : edge ? 'Monitoring on Supabase' : 'Monitoring on GitHub') : s.ready?'Service ready':'Service degraded'; if(cloud && (s.snapshot_stale || s.snapshot_refresh_error)) $('connection').className='badge warning';
  $('discord-channel').textContent='#'+s.discord.channel_label;
  const dbadge=badge(s.discord.enabled?'enabled':'disabled'); $('discord-status').className=dbadge.className; $('discord-status').textContent=s.discord.enabled?'Webhook configured':'Not configured';
  $('test-discord').classList.toggle('hidden',domainOnly); $('test-discord').disabled=!s.local_delivery_enabled; $('test-discord').textContent=cloud ? 'Tests run from GitHub Actions' : 'Send connection test ↗';
  $('delivery-description').textContent=domainOnly?'Only newly added in-scope domains are sent. Program changes and connection tests stay silent.':s.discord.enabled?(s.discord.include_details?'Event details are included in alerts. Delivery retries are automatic.':'Private mode: alerts contain no report titles, source links, or program names.'):'Configure DISCORD_WEBHOOK_URL on the server for your channel.';
  const timing=s.delivery_timing;
  if(edge && timing?.last_sent_at && Number.isFinite(timing.source_to_discord_ms)) {
    $('delivery-description').textContent+=` Last real alert: ${(timing.source_to_discord_ms/1000).toFixed(2)}s after Intigriti's activity timestamp`;
    if(Number.isFinite(timing.api_seen_to_discord_ms)) $('delivery-description').textContent+=`, ${(timing.api_seen_to_discord_ms/1000).toFixed(2)}s after API detection`;
    $('delivery-description').textContent+='.';
  }
  $('delivery-list').replaceChildren(...s.notifications.recent.slice(0,8).map(r=>{const row=text('div','','delivery-row'); const label=text('div',date(r.created_at));label.append(text('small',r.error_code ? r.error_code.replaceAll('_',' ') : `${r.attempts} delivery attempts`));row.append(label,badge(r.state));return row;}));
  $('checkpoints').replaceChildren(...s.collections.map(c=>{const row=text('div','','checkpoint');const label=text('div',`${names[c.provider]} / ${c.name}`);label.append(text('small',`${c.baseline_done?'Baseline saved':'Building baseline'} · ${c.pages} pages · ${date(c.last_success)}`));if(c.error_code)label.append(text('small',c.error_code.replaceAll('_',' ')));row.append(label,badge(c.status));return row;}));
  if(!s.collections.length)$('checkpoints').append(text('p','Checkpoints appear when a provider is configured.','muted'));
  $('updated').textContent=cloud ? `Cloud data: ${date(s.snapshot_updated_at)} · checks for saved updates every 2s` : `Updated ${new Date().toLocaleTimeString()} · auto-refresh 2s`;
}
async function loadEvents() {
  if(domainOnly) return loadDomains();
  const params=new URLSearchParams({limit:pageSize,offset,provider:$('provider-filter').value,severity:$('severity-filter').value,q:$('search').value});
  const data=await api('/api/events?'+params);total=data.total;
  $('result-count').textContent=`${total.toLocaleString()} results`;
  $('events').replaceChildren(...data.items.map(e=>{const row=document.createElement('tr'), title=document.createElement('td'); const link=text('a',e.title); const url=new URL(e.browser_url || e.url); link.title=e.link_label || 'Open provider website'; const allowed=['hackerone.com','bugcrowd.com','intigriti.com','app.intigriti.com']; if(url.protocol==='https:' && allowed.includes(url.hostname)){link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';} title.append(link,text('small',`${e.program} · ${e.collection} · revision ${e.revision}`)); const sev=document.createElement('td');sev.append(text('span',e.severity,'severity '+(['critical','high','medium','low','informational'].includes(e.severity)?e.severity:'')));row.append(title,text('td',({hackerone:'HackerOne',bugcrowd:'Bugcrowd',intigriti:'Intigriti'})[e.provider]),sev,text('td',e.state),text('td',date(e.last_seen)));return row;}));
  $('empty').classList.toggle('hidden',data.items.length>0);
  if(total===0 && ($('search').value || $('provider-filter').value || $('severity-filter').value)){$('empty').replaceChildren(text('div','◇','empty-symbol'),text('h3','No matching events'),text('p','Try a different search or clear your filters.'));}
  else if(total===0){$('empty').replaceChildren(text('div','◇','empty-symbol'),text('h3','Your next signal starts here'),text('p','Configure a provider’s read-only API credentials on the server. The first sync builds your history silently.'));}
  $('previous').disabled=offset===0;$('next').disabled=offset+pageSize>=total;
  $('page-label').textContent=total ? `${offset+1}–${Math.min(offset+pageSize,total)} of ${total}` : 'No events yet';
}
async function refresh(background=false) { if(loading)return;loading=true;try{const update=await api('/api/updates');const marker=JSON.stringify(update);if(background&&update.revision&&marker===lastUpdate)return;const s=await api('/api/status');signedIn(true);renderStatus(s);await loadEvents();lastUpdate=marker;}catch(e){lastUpdate='';if(e.status===401){signedIn(false);}else{notice(e.message);$('connection').textContent='Connection unavailable';$('connection').className='badge error';}}finally{loading=false;} }
$('login-form').addEventListener('submit',async e=>{e.preventDefault();notice();const button=e.target.querySelector('button');button.disabled=true;try{await api('/api/login',{method:'POST',body:JSON.stringify({token:$('token').value})});$('token').value='';await refresh();}catch(err){notice(err.message);}finally{button.disabled=false;}});
$('logout').addEventListener('click',async()=>{try{await api('/api/logout',{method:'POST'});signedIn(false);notice();}catch(e){notice(e.message);}});
$('filters').addEventListener('submit',async e=>{e.preventDefault();offset=0;try{await loadEvents();}catch(err){notice(err.message);}});
$('previous').addEventListener('click',()=>{offset=Math.max(0,offset-pageSize);loadEvents().catch(e=>notice(e.message));});
$('next').addEventListener('click',()=>{offset+=pageSize;loadEvents().catch(e=>notice(e.message));});
$('refresh').addEventListener('click',()=>{notice();refresh();});
$('test-discord').addEventListener('click',async()=>{try{await api('/api/notifications/test',{method:'POST'});notice('Connection test queued. Check this queue and your Discord channel for delivery.');await refresh();}catch(e){notice(e.message);}});
refresh();setInterval(()=>{if(active&&!document.hidden)refresh(true);},2000);
document.addEventListener('visibilitychange',()=>{if(active&&!document.hidden)refresh(true);});

function renderDomainFocus() {
  document.body.classList.add('domain-only');
  document.title='Phinix — Intigriti domains';
  document.querySelector('.nav[href="#activity"] span').textContent='Domains';
  document.querySelector('#integrations .section-heading p').textContent='New scope additions are checked against current eligibility.';
  document.querySelector('#integrations h2').textContent='Intigriti monitoring';
  document.querySelector('.hero h1').textContent='Intigriti testing domains.';
  document.querySelector('.hero p').textContent='Intigriti scope additions, newest first. No general program feed.';
  document.querySelector('#activity h2').textContent='Domains for testing';
  document.querySelector('#activity .panel-heading p').textContent='New additions first, followed by existing scope. Exact URL and path limits stay visible.';
  document.querySelector('.stat > span').textContent='In-scope domains';
  document.querySelector('.stat > small').textContent='Current scope from monitored updates';
  $('provider-filter').classList.add('hidden'); $('severity-filter').classList.add('hidden');
  $('search').placeholder='Search domains…'; $('search').setAttribute('aria-label','Search domains');
  const header=document.querySelector('#activity thead tr');
  header.replaceChildren(...['DOMAIN / EXACT SCOPE','ADDED','LAST CHECKED','SOURCE'].map(x=>text('th',x)));
}
async function loadDomains() {
  const params=new URLSearchParams({limit:pageSize,offset,q:$('search').value});
  const data=await api('/api/domains?'+params); total=data.total;
  $('result-count').textContent=`${total.toLocaleString()} domains`;
  $('events').replaceChildren(...data.items.map(e=>{
    const row=document.createElement('tr'), domain=document.createElement('td');
    domain.append(text('strong',e.domain));
    const copy=text('button','Copy','subtle'); copy.type='button';
    copy.addEventListener('click',()=>navigator.clipboard.writeText(e.domain).then(()=>{copy.textContent='Copied';}).catch(()=>notice('Select the domain text to copy it.')));
    domain.append(copy);
    for(const endpoint of e.endpoints) if(endpoint!==e.domain) domain.append(text('small',endpoint));
    const source=document.createElement('td'), link=text('a','View scope ↗');
    const u=new URL(e.scope_url); if(u.protocol==='https:' && ['app.intigriti.com','intigriti.com'].includes(u.hostname)) {link.href=u.href;link.target='_blank';link.rel='noopener noreferrer';}
    source.append(link); row.append(domain,text('td',e.baseline?'Existing scope':date(e.added_at)),text('td',date(e.checked_at)),source); return row;
  }));
  $('empty').classList.toggle('hidden',data.items.length>0);
  if(!data.items.length) $('empty').replaceChildren(text('div','◇','empty-symbol'),text('h3','No new domains to show yet'),text('p','Scope updates without newly added domains stay silent. New additions will appear here after verification.'));
  $('previous').disabled=offset===0; $('next').disabled=offset+pageSize>=total;
  $('page-label').textContent=total?`${offset+1}–${Math.min(offset+pageSize,total)} of ${total}`:'Waiting for domain additions';
}
