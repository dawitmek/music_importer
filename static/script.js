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
let currentPath='',sortBy='date',sortDir=-1,autoScroll=true;
let selectedFiles = new Set();
let currentItems = []; // Store current view items for select all
let lastSuggestions = []; // Store search results to avoid JSON attribute escaping issues
let isMultiSelectMode = false;
let longPressTimer = null;
let viewMode = 'auto'; // 'auto', 'grid', 'list'
let coverQueue = [];
let processingCover = false;

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
  const batchTotal = s.batch_total || 0;
  const batchDone = s.batch_completed || 0;

  // Update Dashboard Session Stats & Batch Progress
  const batchPct = batchTotal > 0 ? Math.round((batchDone / batchTotal) * 100) : 0;
  const dashBatchPct = document.getElementById('dash-batch-pct');
  const dashBatchFill = document.getElementById('dash-batch-fill');
  const dashBatchText = document.getElementById('dash-batch-text');

  if(dashBatchPct) dashBatchPct.textContent = `${batchPct}%`;
  if(dashBatchFill) dashBatchFill.style.width = `${batchPct}%`;
  if(dashBatchText) dashBatchText.textContent = `Batch: ${batchDone} / ${batchTotal}`;

  if(document.getElementById('dash-done')) document.getElementById('dash-done').textContent = c.length;
  if(document.getElementById('dash-fail')) document.getElementById('dash-fail').textContent = f.length;

  const lastCompleteEl = document.getElementById('dash-last-complete');
  if(lastCompleteEl && s.last_batch_finished_at) {
      const date = new Date(s.last_batch_finished_at * 1000);
      // Automatically converts to browser's local timezone
      lastCompleteEl.textContent = date.toLocaleString([], {
          month: 'short', 
          day: 'numeric',
          hour: '2-digit', 
          minute: '2-digit'
      });
      lastCompleteEl.style.fontSize = '14px';
  }

  // Update Dashboard System Status
  const statusTitle = document.getElementById('dash-status-title');
  const statusDetail = document.getElementById('dash-status-detail');
  const statusSub = document.getElementById('dash-status-sub');
  const statusCover = document.getElementById('dash-status-cover');

  if(a.length > 0){
      const current = a[0];
      statusTitle.textContent = 'DOWNLOADING';
      statusTitle.style.color = 'var(--gold)';
      statusDetail.textContent = `${current.artist} - ${current.title}`;
      statusSub.textContent = q.length > 0 ? `${q.length} IN QUEUE` : 'FINALIZING';
      statusSub.style.color = 'var(--cyan)';
      statusSub.style.fontWeight = '800';
      statusSub.style.background = 'var(--cyan-dim)';
      if(statusCover) {
          statusCover.src = current.cover || `/api/track-cover?artist=${enc(current.artist)}&title=${enc(current.title)}`;
          statusCover.style.display = 'block';
      }
  } else if(q.length > 0) {
      const first = q[0];
      statusTitle.textContent = 'QUEUED';
      statusTitle.style.color = 'var(--cyan)';
      statusDetail.textContent = `${first.artist} - ${first.title}`;
      statusSub.textContent = `${q.length} PENDING`;
      statusSub.style.color = 'var(--cyan)';
      statusSub.style.fontWeight = '800';
      statusSub.style.background = 'var(--cyan-dim)';
      if(statusCover) {
          statusCover.src = first.cover || `/api/track-cover?artist=${enc(first.artist)}&title=${enc(first.title)}`;
          statusCover.style.display = 'block';
      }
  } else {
      statusTitle.textContent = 'IDLE';
      statusTitle.style.color = 'var(--muted)';
      statusDetail.textContent = 'Ready for new tracks';
      statusSub.textContent = 'READY';
      statusSub.style.color = 'var(--muted)';
      statusSub.style.background = 'transparent';
      statusSub.style.fontWeight = '600';
      if(statusCover) statusCover.style.display = 'none';
  }

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
  ql.innerHTML = '';
  
  if(!all.length){
    ql.innerHTML=`<div style="color:var(--dim);font-size:12px;padding:14px;font-family:'JetBrains Mono',monospace;letter-spacing:1px">— queue is empty —</div>`;
    return;
  }

  all.forEach((item, idx) => {
    const status=item.status||(item._cat==='done'?'completed':item._cat==='fail'?'failed':'pending');
    const prog=status==='downloading'?`<div class="q-progress"><div class="q-progress-bar"></div></div>`:'';
    
    const qItem = document.createElement('div');
    qItem.className = `queue-item ${status}`;
    qItem.style.animationDelay = `${idx*.04}s`;
    qItem.innerHTML = `
      <img class="q-cover" src="/api/track-cover?artist=${enc(item.artist)}&title=${enc(item.title)}" onerror="this.src='/api/track-cover'" loading="lazy"/>
      <div class="q-info"><div class="q-title">${esc(item.title)}</div><div class="q-artist">${esc(item.artist||'—')}</div>${prog}</div>
      <span class="q-status ${status}">${status}</span>
      <div class="q-actions"></div>
    `;

    const actions = qItem.querySelector('.q-actions');
    if(['pending','downloading'].includes(status)){
        const stopBtn = document.createElement('button');
        stopBtn.className = 'q-stop-btn';
        stopBtn.innerHTML = '✕';
        stopBtn.title = 'Remove from queue';
        stopBtn.addEventListener('click', () => removeFromQueue(item.id));
        actions.appendChild(stopBtn);
    } else if(status === 'failed'){
        const retryBtn = document.createElement('button');
        retryBtn.className = 'q-retry-btn';
        retryBtn.innerHTML = '↻';
        retryBtn.title = 'Retry download';
        retryBtn.addEventListener('click', () => retryTrack(item.id));
        actions.appendChild(retryBtn);
    }

    ql.appendChild(qItem);
  });
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
  if(!c) return;
  const isVisible = c.offsetParent !== null;
  
  c.innerHTML=logs.map(l=>{
    const d=new Date(l.ts*1000);
    const ts=d.toLocaleTimeString('en-US',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
    return `<div class="log-line"><span class="ts">${ts}</span><span class="level ${l.level}">${l.level}</span><span class="msg">${esc(l.msg)}</span></div>`;
  }).join('');
  
  if(autoScroll && isVisible) c.scrollTop=c.scrollHeight;
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
    list.innerHTML = '';
    
    if(!history.length){
        list.innerHTML = `<div style="padding:10px;color:var(--dim);font-size:11px;font-family:'JetBrains Mono',monospace">No recent activity</div>`;
        return;
    }
    
    history.forEach((h, i) => {
        let label = typeof h.value === 'string' ? h.value : `${h.value.artist} - ${h.value.title}`;
        let icon = h.type === 'search' ? '🔍' : (h.type === 'playlist' ? '📋' : (h.type === 'manual' ? '✏️' : '🎵'));
        
        const item = document.createElement('div');
        item.className = 'history-item';
        item.innerHTML = `
            <span class="history-icon">${icon}</span>
            <span class="history-val">${esc(label)}</span>
        `;
        item.addEventListener('click', () => applyHistory(i));
        list.appendChild(item);
    });
}

function applyHistory(index){
    const history = JSON.parse(localStorage.getItem('mv-history') || '[]');
    const item = history[index];
    if(!item) return;
    if(item.type === 'manual' || item.type === 'track'){
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
                    // Ensure unique ID for background update tracking
                    t.tempId = Math.random().toString(36).substring(7);
                    stagingTracks.push(t);
                    added++;
                    
                    // Fetch cover in background if missing
                    if(!t.cover || t.cover === '') {
                        fetchCoverInBackground(t);
                    }
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

function fetchCoverInBackground(track) {
    coverQueue.push(track);
    if (!processingCover) {
        processCoverQueue();
    }
}

async function processCoverQueue() {
    if (coverQueue.length === 0) {
        processingCover = false;
        return;
    }

    processingCover = true;
    const track = coverQueue.shift();
    
    // Track retry attempts
    if (track.coverRetries === undefined) track.coverRetries = 0;

    try {
        const r = await fetch(`/api/track-cover?artist=${enc(track.artist)}&title=${enc(track.title)}`);
        if (r.ok) {
            const staged = stagingTracks.find(st => st.tempId === track.tempId);
            if (staged) {
                const blob = await r.blob();
                staged.cover = URL.createObjectURL(blob);
                renderStaging();
            }
        } else {
            throw new Error(`Status ${r.status}`);
        }
    } catch (e) {
        console.warn(`Failed to fetch cover for ${track.title} (Attempt ${track.coverRetries + 1}/3)`, e);
        
        if (track.coverRetries < 2) { // 0, 1, 2 = 3 attempts total
            track.coverRetries++;
            // Push back to the end of the queue to try again later
            coverQueue.push(track);
        }
    }

    // Small delay between requests to be gentle. 
    // If we just failed, we might want to wait a bit longer, but sequential processing naturally provides some gap.
    setTimeout(processCoverQueue, 150);
}

function renderSug(tracks){
  lastSuggestions = tracks; 
  sugEl.innerHTML = '';
  
  if(!tracks.length){
    sugEl.innerHTML=`<div style="padding:20px;color:var(--dim);text-align:center;font-size:12px;font-family:'JetBrains Mono',monospace">No results found</div>`;
    sugEl.classList.remove('hidden'); return;
  }

  tracks.forEach((t, idx) => {
    const item = document.createElement('div');
    item.className = 'suggestion-item';
    item.innerHTML = `
      <img class="sug-cover" src="${esc(t.cover||'')}" onerror="this.src='/api/track-cover?artist=${enc(t.artist)}&title=${enc(t.title)}'" loading="lazy"/>
      <div class="sug-info">
        <div class="sug-title">${esc(t.title)}</div>
        <div class="sug-meta">${esc(t.artist)} · ${esc(t.album)}</div>
      </div>
      <span class="sug-dur">${fmtDur(t.duration)}</span>
      <button class="sug-add-btn">+ Stage</button>
    `;
    
    // Add event listeners directly to the elements
    item.addEventListener('click', () => addToStagingIdx(idx));
    
    const btn = item.querySelector('.sug-add-btn');
    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        addToStagingIdx(idx);
    });
    
    sugEl.appendChild(item);
  });
  
  sugEl.classList.remove('hidden');
}

function addToStagingIdx(idx) {
    if (lastSuggestions[idx]) {
        addToStaging(lastSuggestions[idx]);
    }
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
  saveToHistory('track', track);
  toast(`Staged: ${track.title}`,'success');
}

async function retryTrack(id){
    try{
        const r = await fetch('/api/download/retry', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({id})
        });
        const data = await r.json();
        if(data.ok) toast('Retrying track...', 'info');
    } catch(e) { toast('Failed to retry track', 'error'); }
}

function renderStaging(){
  const el=document.getElementById('staging');
  const cnt=document.getElementById('staging-count');
  cnt.textContent=`${stagingTracks.length} track${stagingTracks.length!==1?'s':''} staged`;
  el.innerHTML = '';
  
  if(!stagingTracks.length){
      el.innerHTML=`<div class="staging-empty">No tracks staged — search and add songs above</div>`;
      return;
  }
  
  stagingTracks.forEach((t, i) => {
      const item = document.createElement('div');
      item.className = 'staging-item';
      item.innerHTML = `
        <img class="staging-cover" src="${esc(t.cover||'')}" onerror="this.src='/api/track-cover?artist=${enc(t.artist)}&title=${enc(t.title)}'" loading="lazy"/>
        <div style="flex:1;min-width:0"><div class="staging-title">${esc(t.title)}</div><div class="staging-artist">${esc(t.artist||'—')}</div></div>
        <button class="staging-remove">✕</button>
      `;
      item.querySelector('.staging-remove').addEventListener('click', () => removeStaging(i));
      el.appendChild(item);
  });
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
document.getElementById('retry-failed-btn').addEventListener('click',async()=>{
    // We can either add a new endpoint for 'retry-all' or just call retry for each
    const failedItems = wsStatus.failed || [];
    if(!failedItems.length) { toast('No failed tracks to retry', 'info'); return; }
    for(const item of failedItems){
        await fetch('/api/download/retry', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({id: item.id})
        });
    }
    toast(`Retrying ${failedItems.length} tracks`, 'info');
});

// ── Files ────────────────────────────────
async function loadFiles(path=currentPath){
  currentPath=path;updateBreadcrumb(path);
  clearSelection();
  try{
    const r=await fetch(`/api/files?path=${enc(path)}`);
    const data=await r.json();
    updateDashboardDisk(data);
    currentItems = data.items || [];
    renderFiles(currentItems);
  }catch{toast('Failed to load files','error');}
}

function toggleSelectAll(){
    if (!currentItems || currentItems.length === 0) return;

    const searchInput = document.getElementById('fm-search');
    const filter = (searchInput ? searchInput.value : '').toLowerCase();
    const filtered = currentItems.filter(f => f.name.toLowerCase().includes(filter));
    
    if (filtered.length === 0) return;

    // Check if all visible items are already selected
    const allFilteredSelected = filtered.every(f => selectedFiles.has(f.path));
    
    if(allFilteredSelected){
        filtered.forEach(f => selectedFiles.delete(f.path));
    } else {
        filtered.forEach(f => selectedFiles.add(f.path));
    }
    
    isMultiSelectMode = selectedFiles.size > 0;
    
    // Re-render to ensure UI is perfectly in sync
    renderFiles(currentItems);
    updateBatchToolbar();
}
function updateDashboardDisk(data){
  const disk = data.disk || {total:1, used:0, free:1};
  const folderSize = data.folder_size || 0;
  const pct = Math.round((disk.used / disk.total) * 100);
  
  // Dashboard update: only folder size
  const dashVal = document.getElementById('dash-storage-val');
  if(dashVal) dashVal.textContent = fmtBytes(folderSize);
  
  // Library page: full disk meter
  const fill = document.getElementById('disk-fill');
  const usedText = document.getElementById('disk-used');
  const pctText = document.getElementById('disk-pct');
  
  if(fill) {
      fill.style.width=`${pct}%`;
      const color = pct > 90 ? 'var(--rose)' : pct > 70 ? 'var(--amber)' : 'var(--emerald)';
      fill.style.background = pct > 70 ? color : 'linear-gradient(90deg,var(--cyan),var(--gold))';
      if(pctText) {
          pctText.textContent = `${pct}% USED`;
          pctText.style.color = color;
      }
  }
  if(usedText) {
      usedText.textContent = `${fmtBytes(disk.used)} / ${fmtBytes(disk.total)}`;
      usedText.style.color = 'var(--cyan)';
  }
}

function clearSelection(){
    selectedFiles.clear();
    isMultiSelectMode = false;
    updateBatchToolbar();
    document.querySelectorAll('.file-card.selected').forEach(el => el.classList.remove('selected'));
}

function updateBatchToolbar(){
    const tb = document.getElementById('batch-toolbar');
    const count = document.getElementById('batch-count');
    if(selectedFiles.size > 0){
        tb.classList.remove('hidden');
        count.textContent = `${selectedFiles.size} item${selectedFiles.size !== 1 ? 's' : ''} selected`;
    } else {
        tb.classList.add('hidden');
        isMultiSelectMode = false;
    }
}

function toggleFileSelection(path, cardEl){
    if(selectedFiles.has(path)){
        selectedFiles.delete(path);
        cardEl.classList.remove('selected');
    } else {
        selectedFiles.add(path);
        cardEl.classList.add('selected');
    }
    isMultiSelectMode = selectedFiles.size > 0;
    updateBatchToolbar();
}

function renderFiles(items){
  const filter=document.getElementById('fm-search').value.toLowerCase();
  let filtered=items.filter(f=>f.name.toLowerCase().includes(filter));
  
  filtered.sort((a,b)=>{
    if(a.type!==b.type) return a.type==='dir'?-1:1;
    const cmp=sortBy=== 'name' ? a.name.localeCompare(b.name) : sortBy === 'size' ? a.size - b.size : a.modified - b.modified;
    return cmp*sortDir;
  });

  const grid=document.getElementById('file-grid');
  grid.innerHTML = '';
  
  if(!filtered.length){
      grid.innerHTML=`<div style="color:var(--dim);font-size:12px;font-family:'JetBrains Mono',monospace;padding:20px;grid-column:1/-1;letter-spacing:1px">— no files found —</div>`;
      return;
  }
  
  filtered.forEach((f, idx) => {
    const isDir = f.type === 'dir';
    
    // Determine layout based on viewMode
    let layoutClass = 'grid-layout';
    if(viewMode === 'list') layoutClass = 'list-layout';
    else if(viewMode === 'grid') layoutClass = 'grid-layout';
    else {
        layoutClass = isDir ? 'list-layout' : 'grid-layout';
    }

    const isAudio=['.mp3','.flac','.m4a','.wav','.ogg','.opus'].includes(f.ext);
    const isImage=['.jpg','.jpeg','.png','.webp','.gif'].includes(f.ext);
    
    let iconHTML = isDir ? '📁' : getFileIcon(f.ext);
    if(isImage) {
        const thumbSize = layoutClass === 'list-layout' ? '44px' : '80px';
        iconHTML = `<div style="width:${thumbSize};height:${thumbSize};display:flex;align-items:center;justify-content:center;overflow:hidden;border-radius:var(--radius);background:var(--bg)">
                      <img src="/files/${urlEnc(f.path)}" style="width:100%;height:100%;object-fit:cover;"/>
                    </div>`;
    }

    const card = document.createElement('div');
    card.className = `file-card ${layoutClass} ${selectedFiles.has(f.path) ? 'selected' : ''}`;
    card.style.animationDelay = `${idx*.02}s`;
    card.setAttribute('data-path', f.path);
    
    card.innerHTML = `
      <div class="file-icon">${iconHTML}</div>
      <div class="file-info-wrap">
        <div class="file-name" title="${esc(f.name)}">${esc(f.name)}</div>
        <div class="file-meta">${isDir ? 'folder' : fmtBytes(f.size)}</div>
      </div>
      <div class="file-actions"></div>
    `;

    const actionsContainer = card.querySelector('.file-actions');
    if (isDir) {
        actionsContainer.appendChild(createActionBtn('fa-zip', '🗜', 'Zip', (e) => zipFolder(f.path, e)));
        actionsContainer.appendChild(createActionBtn('fa-del', '🗑', 'Delete', (e) => deleteFile(f.path, e)));
    } else {
        if (isAudio) {
            actionsContainer.appendChild(createActionBtn('fa-play', '▶', 'Play', (e) => playFile(f.path, f.name, e)));
        }
        actionsContainer.appendChild(createActionBtn('fa-dl', '⬇', 'Download', (e) => downloadFile(f.path, e)));
        actionsContainer.appendChild(createActionBtn('fa-rename', '✏', 'Rename', (e) => renameFile(f.path, f.name, e)));
        actionsContainer.appendChild(createActionBtn('fa-del', '🗑', 'Delete', (e) => deleteFile(f.path, e)));
    }

    card.onmousedown = (e) => onFileMouseDown(f.path, e);
    card.onmouseup = (e) => onFileMouseUp(f.path, f.type, e);
    card.onmouseleave = () => onFileMouseLeave();
    card.ontouchstart = (e) => onFileMouseDown(f.path, e);
    card.ontouchend = (e) => onFileMouseUp(f.path, f.type, e);

    grid.appendChild(card);
  });
}

function createActionBtn(cls, icon, title, onClick) {
    const btn = document.createElement('button');
    btn.className = `fa-btn ${cls}`;
    btn.innerHTML = icon;
    btn.title = title;
    btn.addEventListener('click', onClick);
    return btn;
}

function onFileMouseDown(path, e){
    if(e.button !== 0 && e.type !== 'touchstart') return;
    longPressTimer = setTimeout(() => {
        isMultiSelectMode = true;
        const card = e.target.closest('.file-card');
        toggleFileSelection(path, card);
        if(window.navigator.vibrate) window.navigator.vibrate(50);
        longPressTimer = null;
    }, 600);
}

function onFileMouseUp(path, type, e){
    if(longPressTimer){
        clearTimeout(longPressTimer);
        longPressTimer = null;
        if(isMultiSelectMode){
            const card = e.target.closest('.file-card');
            toggleFileSelection(path, card);
        } else {
            fileClick(path, type);
        }
    }
}

function onFileMouseLeave(){
    if(longPressTimer){
        clearTimeout(longPressTimer);
        longPressTimer = null;
    }
}

function fileClick(path,type){
    if(type==='dir') loadFiles(path);
    else {
        const ext = path.split('.').pop().toLowerCase();
        if(['jpg','jpeg','png','webp','gif'].includes(ext)){
            viewImage(path);
        }
    }
}

function viewImage(path){
    const img = document.getElementById('viewer-img');
    img.src = `/files/${urlEnc(path)}`;
    openModal('image-modal');
}
function updateBreadcrumb(path){
  const el=document.getElementById('breadcrumb');
  const parts=path?path.split('/'):[];
  el.innerHTML='';

  const rootSpan = document.createElement('span');
  rootSpan.className = `bread-part ${!parts.length?'active':''}`;
  rootSpan.textContent = '⌂ Root';
  rootSpan.addEventListener('click', () => loadFiles(''));
  el.appendChild(rootSpan);

  parts.forEach((p,i)=>{
    const sep = document.createElement('span');
    sep.className = 'bread-sep';
    sep.textContent = '/';
    el.appendChild(sep);

    const sub=parts.slice(0,i+1).join('/');
    const last=i===parts.length-1;

    const partSpan = document.createElement('span');
    partSpan.className = `bread-part ${last?'active':''}`;
    partSpan.textContent = p;
    partSpan.addEventListener('click', () => loadFiles(sub));
    el.appendChild(partSpan);
  });
}

document.getElementById('fm-search').addEventListener('input',()=>loadFiles());
document.getElementById('view-toggle-btn').addEventListener('click', function(){
    if(viewMode === 'auto') viewMode = 'grid';
    else if(viewMode === 'grid') viewMode = 'list';
    else viewMode = 'auto';
    
    this.textContent = viewMode === 'auto' ? '▦ Auto' : (viewMode === 'grid' ? '▤ Grid' : '☰ List');
    loadFiles();
});
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
  audio.src=`/files/${urlEnc(path)}`;audio.play();
  document.getElementById('player-title').textContent=decodeURIComponent(name.replace(/\.[^.]+$/,''));
  document.getElementById('player-artist').textContent='—';
  document.getElementById('play-btn').innerHTML='⏸';
  document.getElementById('vinyl-disc').classList.add('spinning');
  document.getElementById('eq-bars').classList.add('active');
  
  // path is relative to DOWNLOADS_DIR, e.g. "singles/Artist - Song/song.mp3"
  // folder should be "singles/Artist - Song"
  const parts = path.split('/');
  parts.pop(); // Remove filename
  const folder = parts.join('/');
  
  document.getElementById('player-cover').src=`/api/track-cover?folder=${enc(folder)}`;
}
function downloadFile(path,e){
  e&&e.stopPropagation();
  const a=document.createElement('a');a.href=`/files/${urlEnc(path)}`;a.download=path.split('/').pop();a.click();
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
  
  if(cfg.deps){
      const sr = document.getElementById('dep-streamrip');
      const yt = document.getElementById('dep-ytdlp');
      if(sr) sr.innerHTML = `streamrip: <span style="color:${cfg.deps.streamrip ? 'var(--emerald)' : 'var(--rose)'}">${cfg.deps.streamrip ? 'INSTALLED' : 'MISSING'}</span>`;
      if(yt) yt.innerHTML = `yt-dlp: <span style="color:${cfg.deps.ytdlp ? 'var(--emerald)' : 'var(--rose)'}">${cfg.deps.ytdlp ? 'INSTALLED' : 'MISSING'}</span>`;
  }
  if(cfg.download_path){
      const dp = document.getElementById('cfg-dl-path');
      if(dp) dp.textContent = cfg.download_path;
  }
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
  if(name==='logs'){
      const c = document.getElementById('log-container');
      if(c && autoScroll) c.scrollTop = c.scrollHeight;
  }
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
function urlEnc(path){
    return (path||'').split('/').map(p => encodeURIComponent(p)).join('/');
}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function fmtDur(s){s=Math.floor(s||0);return`${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`}
function fmtBytes(b){if(!b)return'0 B';const u=['B','KB','MB','GB'];const i=Math.floor(Math.log(b)/Math.log(1024));return`${(b/Math.pow(1024,i)).toFixed(1)} ${u[i]}`}
function getFileIcon(ext){
  const m={'.mp3':'🎵','.flac':'🎼','.m4a':'🎵','.wav':'🎧','.ogg':'🎵','.opus':'🎵','.jpg':'🖼','.jpeg':'🖼','.png':'🖼','.gif':'🖼','.zip':'📦','.rar':'📦','.tar':'📦','.txt':'📄','.log':'📄','.json':'📄','.toml':'📄'};
  return m[ext]||'📄';
}

// ── Init ──────────────────────────────────
connectWS();loadConfig();loadFiles('');

document.getElementById('batch-cancel').addEventListener('click', clearSelection);

document.getElementById('batch-delete').addEventListener('click', async () => {
    if(selectedFiles.size === 0) return;
    if(!confirm(`Delete ${selectedFiles.size} selected items?`)) return;
    
    toast(`Deleting ${selectedFiles.size} items...`, 'info');
    const items = Array.from(selectedFiles);
    for(const path of items){
        await fetch('/api/files/delete', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({path})
        });
    }
    toast('Batch delete complete', 'success');
    loadFiles();
});

document.getElementById('batch-zip').addEventListener('click', async () => {
    if(selectedFiles.size === 0) return;
    toast('Creating batch zip...', 'info');
    
    try {
        const r = await fetch('/api/files/zip/batch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({paths: Array.from(selectedFiles)})
        });
        const data = await r.json();
        if(data.ok){
            toast('Batch zip ready!', 'success');
            const a = document.createElement('a');
            a.href = `/files/${data.zip_path}`;
            a.download = data.zip_path.split('/').pop();
            a.click();
            clearSelection();
        } else {
            toast('Batch zip failed', 'error');
        }
    } catch(e) {
        toast('Error creating batch zip', 'error');
    }
});
