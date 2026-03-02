/* ═══════════════════════════════════════════════
   Music Vault Frontend JS
═══════════════════════════════════════════════ */

// ── Starfield canvas ──────────────────────
(function(){
  const c=document.getElementById('bg-canvas');
  const ctx=c.getContext('2d');
  let W,H,stars=[],nebulae=[];
  function resize(){W=c.width=window.innerWidth;H=c.height=window.innerHeight}
  function init(){
    stars=[];
    for(let i=0;i<160;i++) stars.push({
      x:Math.random()*W,y:Math.random()*H,
      r:Math.random()*1.2+.2,a:Math.random(),
      da:(Math.random()-.5)*.007,
      dx:(Math.random()-.5)*.06,
      col:Math.random()>.85?'#00d4ff':Math.random()>.7?'#ffc837':'#c8d8e8'
    });
    nebulae=[];
    for(let i=0;i<5;i++) nebulae.push({
      x:Math.random()*W,y:Math.random()*H,
      r:100+Math.random()*220,
      c:i%2===0?'rgba(0,212,255,':'rgba(255,200,55,',
      a:.018+Math.random()*.025
    });
  }
  function frame(){
    ctx.clearRect(0,0,W,H);
    nebulae.forEach(n=>{
      const g=ctx.createRadialGradient(n.x,n.y,0,n.x,n.y,n.r);
      g.addColorStop(0,n.c+n.a+')');
      g.addColorStop(1,n.c+'0)');
      ctx.fillStyle=g;
      ctx.beginPath();ctx.arc(n.x,n.y,n.r,0,Math.PI*2);ctx.fill();
    });
    stars.forEach(s=>{
      s.a=Math.max(.04,Math.min(1,s.a+s.da));
      if(s.a<=.04||s.a>=1)s.da*=-1;
      s.x+=s.dx;if(s.x<0)s.x=W;if(s.x>W)s.x=0;
      ctx.globalAlpha=s.a;ctx.fillStyle=s.col;
      ctx.beginPath();ctx.arc(s.x,s.y,s.r,0,Math.PI*2);ctx.fill();
    });
    ctx.globalAlpha=1;
    requestAnimationFrame(frame);
  }
  resize();init();frame();
  window.addEventListener('resize',()=>{resize();init();});
})();

// ── State ────────────────────────────────
let ws=null,wsStatus={},stagingTracks=[];
let currentPath='',sortBy='name',sortDir=1,autoScroll=true;

// ── WebSocket ────────────────────────────
function connectWS(){
  const proto=location.protocol==='https:'?'wss':'ws';
  ws=new WebSocket(`${proto}://${location.host}/ws/status`);
  ws.onopen=()=>{
    document.getElementById('ws-dot').className='conn-dot connected';
    document.getElementById('ws-label').textContent='connected';
  };
  ws.onclose=()=>{
    document.getElementById('ws-dot').className='conn-dot';
    document.getElementById('ws-label').textContent='reconnecting…';
    setTimeout(connectWS,3000);
  };
  ws.onmessage=e=>{
    const{event,data}=JSON.parse(e.data);
    if(event==='status'){wsStatus=data;updateUI(data);}
  };
}

function updateUI(s){
  const q=s.queue||[],a=s.active||[],c=s.completed||[],f=s.failed||[];
  animNum('stat-queue',q.length);animNum('stat-active',a.length);
  animNum('stat-done',c.length);animNum('stat-fail',f.length);
  const total=q.length+a.length;
  const badge=document.getElementById('queue-badge');
  badge.textContent=total;badge.classList.toggle('hidden',total===0);
  document.getElementById('ws-dot').className=a.length>0?'conn-dot downloading':'conn-dot connected';
  const all=[
    ...a.map(x=>({...x,_cat:'active'})),...q.map(x=>({...x,_cat:'queue'})),
    ...c.slice(-10).reverse().map(x=>({...x,_cat:'done'})),
    ...f.slice(-5).reverse().map(x=>({...x,_cat:'fail'}))
  ];
  const ql=document.getElementById('queue-list');
  if(!all.length){
    ql.innerHTML=`<div style="color:var(--dim);font-size:12px;padding:14px;font-family:'JetBrains Mono',monospace;letter-spacing:1px">— queue is empty —</div>`;
    return;
  }
  ql.innerHTML=all.map((item,idx)=>{
    const status=item.status||(item._cat==='done'?'completed':item._cat==='fail'?'failed':'pending');
    const prog=status==='downloading'?`<div class="q-progress"><div class="q-progress-bar"></div></div>`:'';
    const isRemovable = ['pending','downloading'].includes(status);
    const stopBtn = isRemovable ? `<button class="q-stop-btn" onclick="removeFromQueue('${item.id}')" title="Remove from queue">✕</button>` : '';

    return `<div class="queue-item ${status}" style="animation-delay:${idx*.04}s">
      <img class="q-cover" src="/api/track-cover?artist=${enc(item.artist)}&title=${enc(item.title)}" onerror="this.src='/api/track-cover'" loading="lazy"/>
      <div class="q-info"><div class="q-title">${esc(item.title)}</div><div class="q-artist">${esc(item.artist||'—')}</div>${prog}</div>
      <span class="q-status ${status}">${status}</span>
      ${stopBtn}
    </div>`;
  }).join('');
  updateLogs(s.logs||[]);
}

async function removeFromQueue(id){
  try{
    const r=await fetch('/api/download/remove',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id})
    });
    const data=await r.json();
    if(data.ok) toast('Removed from queue','info');
  }catch(e){ toast('Failed to remove track','error'); }
}

function animNum(id,v){
  const el=document.getElementById(id);if(!el)return;
  if(parseInt(el.textContent)===v)return;
  el.textContent=v;
  el.style.transition='transform .2s cubic-bezier(.4,0,.2,1)';
  el.style.transform='scale(1.2)';
  setTimeout(()=>{el.style.transform='';},200);
}

function updateLogs(logs){
  const c=document.getElementById('log-container');
  c.innerHTML=logs.map(l=>{
    const d=new Date(l.ts*1000);
    const ts=d.toLocaleTimeString('en-US',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
    return `<div class="log-line"><span class="ts">${ts}</span><span class="level ${l.level}">${l.level}</span><span class="msg">${esc(l.msg)}</span></div>`;
  }).join('');
  if(autoScroll)c.scrollTop=c.scrollHeight;
}

// ── Search ───────────────────────────────
let sTimeout;
const sInput=document.getElementById('search-q');
const sugEl=document.getElementById('suggestions');
const statusEl=document.getElementById('search-status');
const histEl=document.getElementById('history-panel');

function toggleHistory(show){
    if(show===undefined) histEl.classList.toggle('hidden');
    else if(show) histEl.classList.remove('hidden');
    else histEl.classList.add('hidden');
    if(!histEl.classList.contains('hidden')) renderHistory();
}

document.getElementById('history-toggle-btn').addEventListener('click',(e)=>{
    e.stopPropagation();
    toggleHistory();
});

document.addEventListener('click',e=>{
    if(!e.target.closest('#search-area')){
        sugEl.classList.add('hidden');
        toggleHistory(false);
    }
});

function saveToHistory(type, value){
    let history = JSON.parse(localStorage.getItem('mv-history') || '[]');
    // Avoid duplicates of the same value
    history = history.filter(h => JSON.stringify(h.value) !== JSON.stringify(value));
    history.unshift({ type, value, ts: Date.now() });
    localStorage.setItem('mv-history', JSON.stringify(history.slice(0, 30))); // Keep 30 items
}

function renderHistory(){
    const list = document.getElementById('history-list');
    const history = JSON.parse(localStorage.getItem('mv-history') || '[]');
    if(!history.length){
        list.innerHTML = `<div style="padding:10px;color:var(--dim);font-size:11px;font-family:'JetBrains Mono',monospace">No recent activity</div>`;
        return;
    }
    list.innerHTML = history.map((h, i) => {
        let label = typeof h.value === 'string' ? h.value : `${h.value.artist} - ${h.value.title}`;
        let icon = h.type === 'search' ? '🔍' : (h.type === 'playlist' ? '📋' : '✏️');
        return `<div class="history-item" onclick="applyHistory(${i})">
            <span class="history-icon">${icon}</span>
            <span class="history-val">${esc(label)}</span>
        </div>`;
    }).join('');
}

function applyHistory(index){
    const history = JSON.parse(localStorage.getItem('mv-history') || '[]');
    const item = history[index];
    if(!item) return;
    if(item.type === 'manual'){
        addToStaging(item.value);
    } else {
        sInput.value = item.value;
        handleSearchSubmit();
    }
    toggleHistory(false);
}

document.getElementById('clear-history-btn').addEventListener('click', (e)=>{
    e.stopPropagation();
    localStorage.removeItem('mv-history');
    renderHistory();
});

sInput.addEventListener('input',()=>{
  clearTimeout(sTimeout);
  const q=sInput.value.trim();
  statusEl.textContent = '';
  if(!q||q.length<2){sugEl.classList.add('hidden');return;}
  
  // If it looks like a URL, don't show Deezer suggestions
  if(q.startsWith('http')){
      sugEl.classList.add('hidden');
      statusEl.textContent = 'Playlist URL detected. Press Enter or click ⌕ to fetch.';
      return;
  }
  
  sTimeout=setTimeout(()=>doSearch(q),320);
});

sInput.addEventListener('keydown',e=>{
  if(e.key==='Enter') handleSearchSubmit();
  if(e.key==='Escape') {
      sugEl.classList.add('hidden');
      toggleHistory(false);
  }
});

document.getElementById('search-submit').addEventListener('click',handleSearchSubmit);

function handleSearchSubmit(){
    const q=sInput.value.trim();
    if(!q) return;
    if(q.startsWith('http')){
        saveToHistory('playlist', q);
        doPlaylistSearch(q);
    } else {
        saveToHistory('search', q);
        doSearch(q);
    }
    toggleHistory(false);
}

document.addEventListener('click',e=>{if(!e.target.closest('#search-area'))sugEl.classList.add('hidden');});

async function doSearch(q){
  if(!q || q.startsWith('http'))return;
  try{
    const r=await fetch(`/api/search/suggestions?q=${enc(q)}`);
    renderSug(await r.json());
  }catch{toast('Search failed','error');}
}

async function doPlaylistSearch(url){
    statusEl.textContent='Fetching playlist metadata...';
    try{
        const r=await fetch('/api/search/playlist',{
            method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({url})
        });
        const data=await r.json();
        if(data.error){
            statusEl.textContent=`Error: ${data.error}`;
            toast(data.error,'error');
        }else{
            const tracks=data.tracks||[];
            const playlistTitle = data.title || 'Unknown Playlist';
            statusEl.textContent=`Found ${tracks.length} tracks in "${playlistTitle}". Staging…`;
            let added=0;
            tracks.forEach(t=>{
                if(!stagingTracks.find(st=>st.title===t.title&&st.artist===t.artist)){
                    t.playlist_name = playlistTitle;
                    stagingTracks.push(t);
                    added++;
                }
            });
            renderStaging();
            statusEl.textContent=`Added ${added} new tracks from "${playlistTitle}" to staging.`;
            toast(`Added ${added} tracks`,'success');
            sInput.value='';
        }
    }catch(e){
        statusEl.textContent='Failed to fetch playlist';
        toast('Fetch failed','error');
    }
}

function renderSug(tracks){
  if(!tracks.length){
    sugEl.innerHTML=`<div style="padding:20px;color:var(--dim);text-align:center;font-size:12px;font-family:'JetBrains Mono',monospace">No results found</div>`;
    sugEl.classList.remove('hidden');return;
  }
  sugEl.innerHTML=tracks.map(t=>{
    const trackData = JSON.stringify(t);
    return `
    <div class="suggestion-item" onclick='addToStaging(${trackData})'>
      <img class="sug-cover" src="${esc(t.cover||'')}" onerror="this.src='/api/track-cover'" loading="lazy"/>
      <div class="sug-info">
        <div class="sug-title">${esc(t.title)}</div>
        <div class="sug-meta">${esc(t.artist)} · ${esc(t.album)}</div>
      </div>
      <span class="sug-dur">${fmtDur(t.duration)}</span>
      <button class="sug-add-btn" onclick='event.stopPropagation(); addToStaging(${trackData})'>+ Stage</button>
    </div>`;
  }).join('');
  sugEl.classList.remove('hidden');
}

// ── Mode toggle ──────────────────────────
document.getElementById('mode-search').addEventListener('click',()=>{
  setActiveMode('search');
});
document.getElementById('mode-manual').addEventListener('click',()=>{
  setActiveMode('manual');
});

function setActiveMode(mode){
  ['search','manual'].forEach(m=>{
    const btn=document.getElementById(`mode-${m}`);
    const area=document.getElementById(`${m}-area`);
    if(btn){
        if(m===mode) btn.classList.add('active');
        else btn.classList.remove('active');
    }
    if(area){
        if(m===mode) area.classList.remove('hidden');
        else area.classList.add('hidden');
    }
  });
}

document.getElementById('add-manual').addEventListener('click',()=>{
  const artist=document.getElementById('m-artist').value.trim();
  const title=document.getElementById('m-title').value.trim();
  if(!title){toast('Please enter a title','error');return;}
  const track = {title,artist,cover:''};
  addToStaging(track);
  saveToHistory('manual', track);
  document.getElementById('m-artist').value='';document.getElementById('m-title').value='';
});

// ── Staging ──────────────────────────────
function addToStaging(track){
  if(stagingTracks.find(t=>t.title===track.title&&t.artist===track.artist)){toast('Already staged','info');return;}
  stagingTracks.push(track);renderStaging();
  sugEl.classList.add('hidden');
  toast(`Staged: ${track.title}`,'success');
}
function renderStaging(){
  const el=document.getElementById('staging');
  const cnt=document.getElementById('staging-count');
  cnt.textContent=`${stagingTracks.length} track${stagingTracks.length!==1?'s':''} staged`;
  if(!stagingTracks.length){el.innerHTML=`<div class="staging-empty">No tracks staged — search and add songs above</div>`;return;}
  el.innerHTML=stagingTracks.map((t,i)=>`
    <div class="staging-item">
      <img class="staging-cover" src="${esc(t.cover||'')}" onerror="this.src='/api/track-cover'" loading="lazy"/>
      <div style="flex:1;min-width:0"><div class="staging-title">${esc(t.title)}</div><div class="staging-artist">${esc(t.artist||'—')}</div></div>
      <button class="staging-remove" onclick="removeStaging(${i})">✕</button>
    </div>`).join('');
}
function removeStaging(i){stagingTracks.splice(i,1);renderStaging();}
document.getElementById('clear-staging').addEventListener('click',()=>{stagingTracks=[];renderStaging();});
document.getElementById('sync-all').addEventListener('click',async()=>{
  if(!stagingTracks.length){toast('Nothing staged!','error');return;}
  try{
    const r=await fetch('/api/download/playlist',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({tracks:stagingTracks.map(t=>({
          title:t.title,
          artist:t.artist,
          deezer_id:t.id,
          cover:t.cover,
          playlist_name:t.playlist_name
      }))})});
    const data=await r.json();
    toast(`Queued ${data.count} tracks ⚡`,'success');
    stagingTracks=[];renderStaging();
  }catch{toast('Failed to queue tracks','error');}
});
document.getElementById('clear-queue-btn').addEventListener('click',async()=>{
  await fetch('/api/download/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all:true})});
  toast('Queue cleared','info');
});
document.getElementById('stop-queue-btn').addEventListener('click',async()=>{
  await fetch('/api/download/stop',{method:'POST',headers:{'Content-Type':'application/json'}});
  toast('Queue and active downloads stopped','info');
});

// ── Files ────────────────────────────────
async function loadFiles(path=currentPath){
  currentPath=path;updateBreadcrumb(path);
  try{
    const r=await fetch(`/api/files?path=${enc(path)}`);
    const data=await r.json();
    if(data.disk)updateDisk(data.disk);
    renderFiles(data.items||[]);
  }catch{toast('Failed to load files','error');}
}
function updateDisk(disk){
  const pct=Math.round((disk.used/disk.total)*100);
  document.getElementById('disk-fill').style.width=`${pct}%`;
  document.getElementById('disk-fill').style.background=pct>90?'var(--rose)':pct>70?'var(--amber)':'linear-gradient(90deg,var(--cyan),var(--gold))';
  document.getElementById('disk-info').textContent=`${fmtBytes(disk.used)} / ${fmtBytes(disk.total)} (${pct}%)`;
}
function renderFiles(items){
  const filter=document.getElementById('fm-search').value.toLowerCase();
  let filtered=items.filter(f=>f.name.toLowerCase().includes(filter));
  filtered.sort((a,b)=>{
    if(a.type!==b.type)return a.type==='dir'?-1:1;
    const cmp=sortBy==='name'?a.name.localeCompare(b.name):sortBy==='size'?a.size-b.size:a.modified-b.modified;
    return cmp*sortDir;
  });
  const grid=document.getElementById('file-grid');
  if(!filtered.length){grid.innerHTML=`<div style="color:var(--dim);font-size:12px;font-family:'JetBrains Mono',monospace;padding:20px;grid-column:1/-1;letter-spacing:1px">— no files found —</div>`;return;}
  grid.innerHTML=filtered.map((f,idx)=>{
    const icon=f.type==='dir'?'📁':getFileIcon(f.ext);
    const isAudio=['.mp3','.flac','.m4a','.wav','.ogg','.opus'].includes(f.ext);
    const actions=f.type==='dir'?`<div class="file-actions"><button class="fa-btn fa-zip" title="Zip" onclick="zipFolder('${esc(f.path)}',event)">🗜</button><button class="fa-btn fa-del" title="Delete" onclick="deleteFile('${esc(f.path)}',event)">🗑</button></div>`
      :`<div class="file-actions">${isAudio?`<button class="fa-btn fa-play" title="Play" onclick="playFile('${esc(f.path)}','${esc(f.name)}',event)">▶</button>`:''}<button class="fa-btn fa-dl" title="Download" onclick="downloadFile('${esc(f.path)}',event)">⬇</button><button class="fa-btn fa-rename" title="Rename" onclick="renameFile('${esc(f.path)}','${esc(f.name)}',event)">✏</button><button class="fa-btn fa-del" title="Delete" onclick="deleteFile('${esc(f.path)}',event)">🗑</button></div>`;
    return `<div class="file-card" style="animation-delay:${idx*.03}s" onclick="fileClick('${esc(f.path)}','${esc(f.type)}')">
      <div class="file-icon">${icon}</div><div class="file-name" title="${esc(f.name)}">${esc(f.name)}</div>
      <div class="file-meta">${f.type==='dir'?'folder':fmtBytes(f.size)}</div>${actions}</div>`;
  }).join('');
}
function fileClick(path,type){if(type==='dir')loadFiles(path);}
function updateBreadcrumb(path){
  const el=document.getElementById('breadcrumb');
  const parts=path?path.split('/').filter(Boolean):[];
  let html=`<span class="bread-part ${!parts.length?'active':''}" onclick="loadFiles('')">⌂ Root</span>`;
  parts.forEach((p,i)=>{
    const sub=parts.slice(0,i+1).join('/');const last=i===parts.length-1;
    html+=`<span class="bread-sep">/</span><span class="bread-part ${last?'active':''}" onclick="loadFiles('${esc(sub)}')">${esc(p)}</span>`;
  });
  el.innerHTML=html;
}
document.getElementById('fm-search').addEventListener('input',()=>loadFiles());
document.querySelectorAll('.sort-btn').forEach(btn=>{
  btn.addEventListener('click',()=>{
    const s=btn.dataset.sort;
    if(sortBy===s)sortDir*=-1;else{sortBy=s;sortDir=1;}
    document.querySelectorAll('.sort-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');loadFiles();
  });
});

function playFile(path,name,e){
  e&&e.stopPropagation();
  const audio=document.getElementById('audio-el');
  audio.src=`/files/${path}`;audio.play();
  document.getElementById('player-title').textContent=decodeURIComponent(name.replace(/\.[^.]+$/,''));
  document.getElementById('player-artist').textContent='—';
  document.getElementById('play-btn').innerHTML='⏸';
  document.getElementById('vinyl-disc').classList.add('spinning');
  document.getElementById('eq-bars').classList.add('active');
  const folder=path.substring(0,path.lastIndexOf('/'));
  document.getElementById('player-cover').src=`/api/track-cover?folder=${enc('/downloads/'+folder)}`;
}
function downloadFile(path,e){
  e&&e.stopPropagation();
  const a=document.createElement('a');a.href=`/files/${path}`;a.download=path.split('/').pop();a.click();
}
async function renameFile(path,name,e){
  e&&e.stopPropagation();
  document.getElementById('rename-input').value=name;openModal('rename-modal');
  document.getElementById('rename-confirm').onclick=async()=>{
    const n=document.getElementById('rename-input').value.trim();if(!n)return;
    await fetch('/api/files/rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path,new_name:n})});
    closeModal('rename-modal');toast('Renamed!','success');loadFiles();
  };
}
async function deleteFile(path,e){
  e&&e.stopPropagation();
  if(!confirm(`Delete "${path.split('/').pop()}"?`))return;
  await fetch('/api/files/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})});
  toast('Deleted','info');loadFiles();
}
async function zipFolder(path,e){
  e&&e.stopPropagation();toast('Creating zip…','info');
  const r=await fetch('/api/files/zip',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})});
  const data=await r.json();
  if(data.ok){toast('Zip ready!','success');const a=document.createElement('a');a.href=`/files/${data.zip_path}`;a.download=data.zip_path.split('/').pop();a.click();}
}

// ── Player ───────────────────────────────
const audio=document.getElementById('audio-el');
const playBtn=document.getElementById('play-btn');
const progressFill=document.getElementById('progress-fill');
const progressBar=document.getElementById('progress-bar');
const volSlider=document.getElementById('volume');
playBtn.addEventListener('click',()=>{
  if(audio.paused){
    audio.play();playBtn.innerHTML='⏸';
    document.getElementById('vinyl-disc').classList.add('spinning');
    document.getElementById('eq-bars').classList.add('active');
  }else{
    audio.pause();playBtn.innerHTML='▶';
    document.getElementById('vinyl-disc').classList.remove('spinning');
    document.getElementById('eq-bars').classList.remove('active');
  }
});
audio.addEventListener('timeupdate',()=>{
  if(!audio.duration)return;
  progressFill.style.width=`${(audio.currentTime/audio.duration)*100}%`;
  document.getElementById('curr-time').textContent=fmtDur(audio.currentTime);
});
audio.addEventListener('loadedmetadata',()=>{document.getElementById('total-time').textContent=fmtDur(audio.duration);});
audio.addEventListener('ended',()=>{
  playBtn.innerHTML='▶';
  document.getElementById('vinyl-disc').classList.remove('spinning');
  document.getElementById('eq-bars').classList.remove('active');
  progressFill.style.width='0%';
});
progressBar.addEventListener('click',e=>{
  const rect=progressBar.getBoundingClientRect();
  audio.currentTime=((e.clientX-rect.left)/rect.width)*audio.duration;
});
volSlider.addEventListener('input',()=>{
  audio.volume=volSlider.value;
  document.getElementById('vol-icon').textContent=volSlider.value>.5?'🔊':volSlider.value>0?'🔉':'🔇';
});
audio.volume=.8;

// ── Config ────────────────────────────────
async function loadConfig(){
  const r=await fetch('/api/config');const cfg=await r.json();
  document.getElementById('cfg-arl').value=cfg.arl||'';
  document.getElementById('cfg-quality').value=cfg.quality||'MP3_320';
}
document.getElementById('save-config-btn').addEventListener('click',async()=>{
  const arl=document.getElementById('cfg-arl').value.trim();
  const quality=document.getElementById('cfg-quality').value;
  await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({arl,quality})});
  toast('Configuration saved!','success');
});

// ── Tabs ──────────────────────────────────
function showTab(name){
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.getElementById(`tab-${name}`).classList.add('active');
  document.querySelector(`[data-tab="${name}"]`).classList.add('active');
  if(name==='files')loadFiles();
  if(name==='config')loadConfig();
}
document.querySelectorAll('.nav-item').forEach(item=>item.addEventListener('click',()=>showTab(item.dataset.tab)));

document.getElementById('clear-logs-btn').addEventListener('click',()=>document.getElementById('log-container').innerHTML='');
document.getElementById('auto-scroll-btn').addEventListener('click',function(){
  autoScroll=!autoScroll;
  this.textContent=autoScroll?'⬇ Auto-scroll ON':'⬇ Auto-scroll OFF';
  this.style.color=autoScroll?'var(--emerald)':'var(--muted)';
});

// ── Modals ────────────────────────────────
function openModal(id){document.getElementById(id).classList.add('open');}
function closeModal(id){document.getElementById(id).classList.remove('open');}
document.querySelectorAll('.modal-overlay').forEach(m=>m.addEventListener('click',e=>{if(e.target===m)m.classList.remove('open');}));

// ── Toast ─────────────────────────────────
function toast(msg,type='info'){
  const c=document.getElementById('toasts');
  const el=document.createElement('div');
  el.className=`toast ${type}`;el.textContent=msg;c.appendChild(el);
  const t=setTimeout(remove,3200);
  function remove(){el.classList.add('toast-out');setTimeout(()=>el.remove(),300);}
  el.addEventListener('click',()=>{clearTimeout(t);remove();});
}

// ── Utils ─────────────────────────────────
function enc(s){return encodeURIComponent(s||'')}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function fmtDur(s){s=Math.floor(s||0);return`${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`}
function fmtBytes(b){if(!b)return'0 B';const u=['B','KB','MB','GB'];const i=Math.floor(Math.log(b)/Math.log(1024));return`${(b/Math.pow(1024,i)).toFixed(1)} ${u[i]}`}
function getFileIcon(ext){
  const m={'.mp3':'🎵','.flac':'🎼','.m4a':'🎵','.wav':'🎧','.ogg':'🎵','.opus':'🎵','.jpg':'🖼','.jpeg':'🖼','.png':'🖼','.gif':'🖼','.zip':'📦','.rar':'📦','.tar':'📦','.txt':'📄','.log':'📄','.json':'📄','.toml':'📄'};
  return m[ext]||'📄';
}

// ── Init ──────────────────────────────────
connectWS();loadConfig();
