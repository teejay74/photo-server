const state = {
  items: [], total: 0, offset: 0, type: 'all', query: '', year: null, month: null,
  timeline: [], albums: [], albumId: null, albumName: '', loading: false, hasMore: true,
  favoritesOnly: false, current: -1, controller: null, map: null, mapLayer: null
};

const gallery = document.querySelector('#gallery');
const sentinel = document.querySelector('#sentinel');
const viewer = document.querySelector('#viewer');
const months = new Intl.DateTimeFormat('ru-RU', {month:'long', year:'numeric'});
const monthNames = Array.from({length:12},(_,i)=>new Intl.DateTimeFormat('ru-RU',{month:'short'}).format(new Date(2020,i,1)).replace('.',''));
const fullDate = new Intl.DateTimeFormat('ru-RU', {day:'numeric', month:'long', year:'numeric', hour:'2-digit', minute:'2-digit'});
const tileDate = new Intl.DateTimeFormat('ru-RU', {day:'numeric', month:'short', year:'numeric'});
const size = n => n < 1048576 ? `${Math.round(n/1024)} КБ` : `${(n/1048576).toFixed(1)} МБ`;

function debounce(fn, wait=300){ let timer; return (...args) => { clearTimeout(timer); timer=setTimeout(()=>fn(...args),wait); }; }
function escapeHtml(value){ const div=document.createElement('div'); div.textContent=value??''; return div.innerHTML; }
function toast(message){ const root=document.querySelector('#toast'); root.textContent=message; root.classList.add('show'); clearTimeout(toast.timer); toast.timer=setTimeout(()=>root.classList.remove('show'),2300); }

async function api(url, options={}){
  const response=await fetch(url,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});
  const data=await response.json().catch(()=>({}));
  if(!response.ok) throw new Error(data.error||`Ошибка ${response.status}`);
  return data;
}

async function load(reset=false){
  if(reset){
    state.controller?.abort(); state.items=[]; state.offset=0; state.hasMore=true; state.loading=false; gallery.innerHTML='';
  }
  if(state.loading || !state.hasMore) return;
  state.loading=true; sentinel.hidden=false; const controller=new AbortController(); state.controller=controller;
  const params=new URLSearchParams({offset:state.offset,limit:60,type:state.type,q:state.query});
  if(state.year) params.set('year',state.year);
  if(state.month) params.set('month',state.month);
  if(state.favoritesOnly) params.set('favorite','1');
  if(state.albumId) params.set('album',state.albumId);
  try{
    const response=await fetch('/api/photos?'+params,{signal:controller.signal});
    if(!response.ok) throw new Error('Ошибка загрузки');
    const data=await response.json();
    state.items.push(...data.items); state.total=data.total; state.offset+=data.items.length; state.hasMore=data.hasMore;
    render();
  }catch(error){
    if(error.name!=='AbortError') gallery.innerHTML=`<div class="empty"><h2>Не удалось загрузить фотографии</h2><p>${escapeHtml(error.message)}</p></div>`;
  }finally{
    if(state.controller===controller){ state.loading=false; sentinel.hidden=!state.hasMore; }
  }
}

function render(){
  gallery.innerHTML=''; let lastMonth='';
  for(const item of state.items){
    if(item.month!==lastMonth){
      const title=document.createElement('div'); title.className='month-title';
      title.innerHTML=`${months.format(new Date(item.taken))}<span>${item.month}</span>`; gallery.append(title); lastMonth=item.month;
    }
    const tile=document.createElement('figure'); tile.className='tile'; tile.dataset.id=item.id;
    const visual=item.type==='image'
      ? `<img loading="lazy" src="${item.thumbUrl}" alt="${escapeHtml(item.name)}">`
      : `<video muted preload="metadata" src="${item.mediaUrl}#t=0.1"></video><span class="video-badge">▶</span>`;
    tile.innerHTML=`${visual}${item.favorite?'<span class="favorite-badge">♥</span>':''}<time class="tile-date" datetime="${item.taken}">${tileDate.format(new Date(item.taken))}</time><figcaption class="tile-overlay"><span class="tile-name">${escapeHtml(item.name)}</span><span>${size(item.size)}</span></figcaption>`;
    tile.addEventListener('click',()=>openViewer(state.items.findIndex(value=>value.id===item.id)));
    tile.querySelector('img')?.addEventListener('load',event=>event.currentTarget.classList.add('loaded'));
    gallery.append(tile);
  }
  document.querySelector('#empty').hidden=state.items.length>0;
  document.querySelector('#resultCount').textContent=`${state.total.toLocaleString('ru-RU')} файлов`;
  renderCollectionState();
}

function renderCollectionState(){
  const favoriteButton=document.querySelector('#favoritesButton');
  favoriteButton.textContent=state.favoritesOnly?'♥':'♡';
  favoriteButton.classList.toggle('active',state.favoritesOnly);
  const chip=document.querySelector('#collectionChip');
  chip.hidden=!state.favoritesOnly&&!state.albumId;
  document.querySelector('#collectionLabel').textContent=state.favoritesOnly?'Избранное':state.albumName;
}

function renderTimeline(){
  const root=document.querySelector('#timelineYears'); root.innerHTML='';
  document.querySelector('#timelineAll').classList.toggle('active',!state.year);
  for(const entry of state.timeline){
    const button=document.createElement('button'); button.className='timeline-year'+(state.year===entry.year?' active':'');
    button.textContent=entry.year; button.title=`${entry.year}: ${entry.count.toLocaleString('ru-RU')} файлов`;
    button.addEventListener('click',()=>selectYear(entry,button)); root.append(button);
  }
  const chip=document.querySelector('#periodChip'); chip.hidden=!state.year;
  if(state.year) document.querySelector('#periodLabel').textContent=state.month?`${monthNames[state.month-1]} ${state.year}`:`${state.year} год`;
}

function selectYear(entry,anchor){ state.year=entry.year; state.month=null; showMonths(entry,anchor); renderTimeline(); load(true); }
function showMonths(entry,anchor){
  const picker=document.querySelector('#monthPicker');
  picker.innerHTML=`<div class="month-picker-header"><strong>${entry.year}</strong><button aria-label="Закрыть">×</button></div><div class="month-grid"></div>`;
  const counts=new Map(entry.months.map(value=>[value.month,value.count])); const grid=picker.querySelector('.month-grid');
  for(let number=1;number<=12;number++){
    if(!counts.has(number)) continue;
    const button=document.createElement('button'); button.className='month-button'+(state.month===number?' active':'');
    button.innerHTML=`${monthNames[number-1]}<small>${counts.get(number)}</small>`;
    button.addEventListener('click',()=>{ state.year=entry.year; state.month=number; picker.hidden=true; renderTimeline(); load(true); }); grid.append(button);
  }
  picker.querySelector('.month-picker-header button').addEventListener('click',()=>picker.hidden=true);
  picker.hidden=false; const rect=anchor.getBoundingClientRect(); picker.style.top=`${Math.min(innerHeight-picker.offsetHeight-12,Math.max(74,rect.top-30))}px`;
}
function clearPeriod(){ state.year=null; state.month=null; document.querySelector('#monthPicker').hidden=true; renderTimeline(); load(true); }
function clearCollection(){ state.favoritesOnly=false; state.albumId=null; state.albumName=''; renderCollectionState(); load(true); }

function openViewer(index){
  if(index<0||index>=state.items.length) return;
  state.current=index; const item=state.items[index];
  document.querySelector('#viewerName').textContent=item.name;
  const metadata=[fullDate.format(new Date(item.taken)),size(item.size)];
  if(item.width&&item.height) metadata.push(`${item.width}×${item.height}`);
  if(item.cameraModel) metadata.push(item.cameraModel);
  if(item.latitude!=null) metadata.push('есть геопозиция');
  document.querySelector('#viewerMeta').textContent=metadata.join(' · ');
  document.querySelector('#downloadCurrent').href='/download/'+item.path.split('/').map(encodeURIComponent).join('/');
  document.querySelector('#favoriteCurrent').textContent=item.favorite?'♥':'♡';
  document.querySelector('#albumCurrent').classList.toggle('active',item.albumIds.length>0);
  document.querySelector('#viewerStage').innerHTML=item.type==='image'
    ? `<img src="${item.mediaUrl}" alt="${escapeHtml(item.name)}">`
    : `<video src="${item.mediaUrl}" controls autoplay playsinline></video>`;
  if(!viewer.open) viewer.showModal();
}

function move(direction){
  const next=state.current+direction;
  if(next>=0&&next<state.items.length) openViewer(next);
  else if(next>=state.items.length&&state.hasMore) load().then(()=>openViewer(next));
}

async function toggleFavorite(){
  const item=state.items[state.current]; if(!item) return;
  try{
    const result=await api(`/api/favorites/${item.id}`,{method:'POST',body:JSON.stringify({favorite:!item.favorite})});
    item.favorite=result.favorite; toast(result.favorite?'Добавлено в избранное':'Удалено из избранного');
    if(state.favoritesOnly&&!result.favorite){ viewer.close(); load(true); } else { openViewer(state.current); render(); }
  }catch(error){ toast(error.message); }
}

async function importBrowserFavorites(){
  if(localStorage.getItem('photo-favorites-imported-v2')) return;
  let ids=[];
  try{ ids=JSON.parse(localStorage.getItem('photo-favorites')||'[]'); }catch(_error){}
  if(Array.isArray(ids)&&ids.length){
    try{ await api('/api/favorites/import',{method:'POST',body:JSON.stringify({photoIds:ids})}); }catch(_error){ return; }
  }
  localStorage.setItem('photo-favorites-imported-v2','1');
}

async function fetchAlbums(){
  const data=await api('/api/albums'); state.albums=data.albums; renderAlbums();
}

function renderAlbums(){
  const root=document.querySelector('#albumList'); root.innerHTML='';
  if(!state.albums.length){ root.innerHTML='<div class="album-empty">Альбомов пока нет.<br>Создайте первый альбом выше.</div>'; return; }
  for(const album of state.albums){
    const card=document.createElement('article'); card.className='album-card';
    const cover=album.coverUrl?`<img class="album-cover" src="${album.coverUrl}" alt="">`:'<span class="album-cover empty-cover">▣</span>';
    card.innerHTML=`${cover}<div><strong>${escapeHtml(album.name)}</strong><small>${album.count.toLocaleString('ru-RU')} фото</small></div><button class="album-delete" title="Удалить альбом" aria-label="Удалить альбом">×</button>`;
    card.addEventListener('click',()=>selectAlbum(album));
    card.querySelector('.album-delete').addEventListener('click',async event=>{
      event.stopPropagation(); if(!confirm(`Удалить альбом «${album.name}»? Фотографии останутся в архиве.`)) return;
      try{ await api(`/api/albums/${album.id}`,{method:'DELETE'}); if(state.albumId===album.id) clearCollection(); await fetchAlbums(); toast('Альбом удалён'); }catch(error){ toast(error.message); }
    });
    root.append(card);
  }
}

function selectAlbum(album){
  state.albumId=album.id; state.albumName=album.name; state.favoritesOnly=false;
  document.querySelector('#albumsDialog').close(); renderCollectionState(); load(true);
}

async function createAlbum(event){
  event.preventDefault(); const input=document.querySelector('#albumName'); const name=input.value.trim(); if(!name) return;
  try{ await api('/api/albums',{method:'POST',body:JSON.stringify({name})}); input.value=''; await fetchAlbums(); toast('Альбом создан'); }
  catch(error){ toast(error.message); }
}

function openAlbumPicker(){
  const item=state.items[state.current]; if(!item) return;
  const root=document.querySelector('#albumPickerList'); root.innerHTML='';
  if(!state.albums.length){ root.innerHTML='<div class="album-empty">Сначала создайте альбом.</div>'; }
  for(const album of state.albums){
    const included=item.albumIds.includes(album.id); const button=document.createElement('button');
    button.className='album-picker-button'+(included?' active':'');
    button.innerHTML=`<span>${escapeHtml(album.name)}</span><span>${included?'✓':'＋'}</span>`;
    button.addEventListener('click',async()=>{
      try{
        const result=await api(`/api/albums/${album.id}/photos/${item.id}`,{method:'POST',body:JSON.stringify({included:!included})});
        item.albumIds=result.included?[...new Set([...item.albumIds,album.id])]:item.albumIds.filter(id=>id!==album.id);
        await fetchAlbums(); openAlbumPicker(); toast(result.included?'Добавлено в альбом':'Удалено из альбома');
      }catch(error){ toast(error.message); }
    }); root.append(button);
  }
  const dialog=document.querySelector('#albumPickerDialog'); if(!dialog.open) dialog.showModal();
}

async function openMap(){
  const dialog=document.querySelector('#mapDialog'); dialog.showModal(); const status=document.querySelector('#mapStatus');
  if(!window.L){ status.textContent='Не удалось загрузить модуль карты. Проверьте доступ в интернет.'; return; }
  status.textContent='Загружаем координаты…';
  try{
    const data=await api('/api/map'); status.textContent=`${data.items.length.toLocaleString('ru-RU')} фотографий с координатами`;
    if(!state.map){
      state.map=L.map('mapCanvas',{zoomControl:true});
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap'}).addTo(state.map);
      state.mapLayer=L.layerGroup().addTo(state.map);
    }
    state.mapLayer.clearLayers(); const bounds=[];
    for(const item of data.items){
      const point=[item.latitude,item.longitude]; bounds.push(point);
      const marker=L.circleMarker(point,{radius:6,className:'photo-marker',weight:2});
      marker.bindPopup(`<div class="map-popup"><img src="${item.thumbUrl}" alt=""><strong>${escapeHtml(item.name)}</strong><small>${fullDate.format(new Date(item.taken))}</small></div>`,{maxWidth:210});
      marker.addTo(state.mapLayer);
    }
    setTimeout(()=>{ state.map.invalidateSize(); if(bounds.length) state.map.fitBounds(bounds,{padding:[30,30],maxZoom:14}); else state.map.setView([30,20],2); },50);
  }catch(error){ status.textContent=error.message; }
}

document.querySelectorAll('.tab').forEach(tab=>tab.addEventListener('click',()=>{
  document.querySelector('.tab.active').classList.remove('active'); tab.classList.add('active'); state.type=tab.dataset.type; load(true);
}));
document.querySelector('#searchButton').addEventListener('click',()=>{ document.querySelector('#searchWrap').classList.add('open'); document.querySelector('#searchInput').focus(); });
document.querySelector('#searchInput').addEventListener('input',debounce(event=>{ state.query=event.target.value; load(true); }));
document.querySelector('#favoritesButton').addEventListener('click',()=>{ state.favoritesOnly=!state.favoritesOnly; state.albumId=null; state.albumName=''; renderCollectionState(); load(true); });
document.querySelector('#albumsButton').addEventListener('click',async()=>{ try{ await fetchAlbums(); document.querySelector('#albumsDialog').showModal(); }catch(error){toast(error.message);} });
document.querySelector('#mapButton').addEventListener('click',openMap);
document.querySelector('#albumForm').addEventListener('submit',createAlbum);
document.querySelector('#timelineAll').addEventListener('click',clearPeriod);
document.querySelector('#periodChip').addEventListener('click',clearPeriod);
document.querySelector('#collectionChip').addEventListener('click',clearCollection);
document.querySelector('#closeViewer').addEventListener('click',()=>viewer.close());
document.querySelector('#prevItem').addEventListener('click',()=>move(-1));
document.querySelector('#nextItem').addEventListener('click',()=>move(1));
document.querySelector('#favoriteCurrent').addEventListener('click',toggleFavorite);
document.querySelector('#albumCurrent').addEventListener('click',openAlbumPicker);
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>document.querySelector('#'+button.dataset.close).close()));

document.addEventListener('keydown',event=>{
  if(event.key==='Escape'&&document.querySelector('#searchWrap').classList.contains('open')&&!viewer.open) document.querySelector('#searchWrap').classList.remove('open');
  if(viewer.open&&event.key==='ArrowLeft') move(-1);
  if(viewer.open&&event.key==='ArrowRight') move(1);
});
[viewer,...document.querySelectorAll('.panel-dialog')].forEach(dialog=>dialog.addEventListener('click',event=>{ if(event.target===dialog) dialog.close(); }));
new IntersectionObserver(entries=>{ if(entries[0].isIntersecting) load(); },{rootMargin:'600px'}).observe(sentinel);

async function initialize(){
  try{
    await importBrowserFavorites();
    const [stats,timeline]=await Promise.all([api('/api/stats'),api('/api/timeline'),fetchAlbums()]);
    document.querySelector('#summary').textContent=`${stats.images.toLocaleString('ru-RU')} фотографий · ${stats.videos.toLocaleString('ru-RU')} видео · ${stats.located.toLocaleString('ru-RU')} на карте`;
    state.timeline=timeline.years; renderTimeline();
  }catch(error){ toast(error.message); }
  load();
}
initialize();
