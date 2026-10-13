const state = {
  items: [], total: 0, offset: 0, type: 'all', query: '', year: null, month: null,
  timeline: [], albums: [], albumId: null, albumName: '', loading: false, hasMore: true,
  favoritesOnly: false, onThisDay: false, current: -1, controller: null, map: null, mapLayer: null,
  filters: {date_from:'',date_to:'',camera:'',gps:'',min_mb:'',max_mb:''},
  selecting: false, selected: new Set(), albumTargetIds: []
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
  if(state.onThisDay){ const now=new Date(); params.set('month',now.getMonth()+1); params.set('day',now.getDate()); params.set('before_year',now.getFullYear()-1); }
  for(const [key,value] of Object.entries(state.filters)) if(value!=='') params.set(key,value);
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
    const tile=document.createElement('figure'); tile.className='tile'+(state.selected.has(item.id)?' selected':''); tile.dataset.id=item.id;
    const visual=item.type==='image'
      ? `<img loading="lazy" src="${item.thumbUrl}" alt="${escapeHtml(item.name)}">`
      : `<video muted preload="metadata" src="${item.mediaUrl}#t=0.1"></video><span class="video-badge">▶</span>`;
    tile.innerHTML=`${visual}<span class="selection-check">${state.selected.has(item.id)?'✓':''}</span>${item.favorite?'<span class="favorite-badge">♥</span>':''}<time class="tile-date" datetime="${item.taken}">${tileDate.format(new Date(item.taken))}</time><figcaption class="tile-overlay"><span class="tile-name">${escapeHtml(item.name)}</span><span>${size(item.size)}</span></figcaption>`;
    tile.addEventListener('click',()=>state.selecting?toggleSelection(item.id):openViewer(state.items.findIndex(value=>value.id===item.id)));
    tile.querySelector('img')?.addEventListener('load',event=>event.currentTarget.classList.add('loaded'));
    gallery.append(tile);
  }
  document.querySelector('#empty').hidden=state.items.length>0;
  document.querySelector('#resultCount').textContent=`${state.total.toLocaleString('ru-RU')} файлов`;
  renderCollectionState();
  renderSelection();
}

function renderCollectionState(){
  const favoriteButton=document.querySelector('#favoritesButton');
  favoriteButton.textContent=state.favoritesOnly?'♥':'♡';
  favoriteButton.classList.toggle('active',state.favoritesOnly);
  const chip=document.querySelector('#collectionChip');
  const activeFilters=Object.values(state.filters).filter(Boolean).length;
  chip.hidden=!state.favoritesOnly&&!state.albumId&&!state.onThisDay&&!activeFilters;
  document.querySelector('#collectionLabel').textContent=state.onThisDay?'В этот день':state.favoritesOnly?'Избранное':state.albumId?state.albumName:`Фильтры: ${activeFilters}`;
  document.querySelector('#todayButton').classList.toggle('active',state.onThisDay);
  document.querySelector('#filtersButton').classList.toggle('active',Boolean(activeFilters));
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

function selectYear(entry,anchor){ state.year=entry.year; state.month=null; state.onThisDay=false; showMonths(entry,anchor); renderTimeline(); renderCollectionState(); load(true); }
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
function clearPeriod(){ state.year=null; state.month=null; state.onThisDay=false; document.querySelector('#monthPicker').hidden=true; renderTimeline(); renderCollectionState(); load(true); }
function clearCollection(){ state.favoritesOnly=false; state.albumId=null; state.albumName=''; state.onThisDay=false; state.filters={date_from:'',date_to:'',camera:'',gps:'',min_mb:'',max_mb:''}; renderCollectionState(); load(true); }

function toggleSelection(id){
  if(state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
  render();
}
function setSelecting(value){
  state.selecting=value; if(!value) state.selected.clear();
  document.body.classList.toggle('selecting',value); document.querySelector('#selectButton').classList.toggle('active',value); render();
}
function renderSelection(){
  const bar=document.querySelector('#selectionBar'); bar.hidden=!state.selecting;
  document.querySelector('#selectionCount').textContent=`${state.selected.size.toLocaleString('ru-RU')} выбрано`;
  bar.querySelectorAll('button:not(#selectCancel)').forEach(button=>button.disabled=!state.selected.size);
}
async function bulkFavorite(){
  const ids=[...state.selected]; if(!ids.length) return;
  try{
    const result=await api('/api/favorites/bulk',{method:'POST',body:JSON.stringify({photoIds:ids,favorite:true})});
    state.items.forEach(item=>{ if(state.selected.has(item.id)) item.favorite=true; }); toast(`В избранное добавлено: ${result.count}`); setSelecting(false);
  }catch(error){ toast(error.message); }
}
async function downloadSelection(){
  const ids=[...state.selected]; if(!ids.length) return; toast('Готовим ZIP…');
  try{
    const response=await fetch('/api/download/zip',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({photoIds:ids})});
    if(!response.ok){ const data=await response.json().catch(()=>({})); throw new Error(data.error||`Ошибка ${response.status}`); }
    const blob=await response.blob(); const url=URL.createObjectURL(blob); const link=document.createElement('a');
    link.href=url; link.download='photodom-selection.zip'; document.body.append(link); link.click(); link.remove(); setTimeout(()=>URL.revokeObjectURL(url),30000);
    toast('Архив скачан'); setSelecting(false);
  }catch(error){ toast(error.message); }
}

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
  state.albumId=album.id; state.albumName=album.name; state.favoritesOnly=false; state.onThisDay=false;
  document.querySelector('#albumsDialog').close(); renderCollectionState(); load(true);
}

async function createAlbum(event){
  event.preventDefault(); const input=document.querySelector('#albumName'); const name=input.value.trim(); if(!name) return;
  try{ await api('/api/albums',{method:'POST',body:JSON.stringify({name})}); input.value=''; await fetchAlbums(); toast('Альбом создан'); }
  catch(error){ toast(error.message); }
}

function openAlbumPicker(targetIds){
  const item=state.items[state.current];
  const isBulk=Array.isArray(targetIds); const ids=isBulk?targetIds:(item?[item.id]:[]); if(!ids.length) return;
  state.albumTargetIds=ids;
  const root=document.querySelector('#albumPickerList'); root.innerHTML='';
  if(!state.albums.length){ root.innerHTML='<div class="album-empty">Сначала создайте альбом.</div>'; }
  for(const album of state.albums){
    const included=!isBulk&&item?.albumIds.includes(album.id); const button=document.createElement('button');
    button.className='album-picker-button'+(included?' active':'');
    button.innerHTML=`<span>${escapeHtml(album.name)}</span><span>${included?'✓':'＋'}</span>`;
    button.addEventListener('click',async()=>{
      try{
        if(!isBulk){
          const result=await api(`/api/albums/${album.id}/photos/${ids[0]}`,{method:'POST',body:JSON.stringify({included:!included})});
          if(item) item.albumIds=result.included?[...new Set([...item.albumIds,album.id])]:item.albumIds.filter(id=>id!==album.id);
          await fetchAlbums(); openAlbumPicker(); toast(result.included?'Добавлено в альбом':'Удалено из альбома');
        }else{
          const result=await api(`/api/albums/${album.id}/photos`,{method:'POST',body:JSON.stringify({photoIds:ids,included:true})});
          document.querySelector('#albumPickerDialog').close(); await fetchAlbums(); toast(`В альбом добавлено: ${result.count}`); setSelecting(false);
        }
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

async function openFilters(){
  try{
    const [options,backups]=await Promise.all([api('/api/filters'),api('/api/backups')]);
    const camera=document.querySelector('#filterCamera'); camera.innerHTML='<option value="">Любая камера</option>';
    for(const entry of options.cameras){ const option=document.createElement('option'); option.value=entry.value; option.textContent=`${entry.value} · ${entry.count}`; camera.append(option); }
    for(const [key,id] of Object.entries({date_from:'filterDateFrom',date_to:'filterDateTo',camera:'filterCamera',gps:'filterGps',min_mb:'filterMin',max_mb:'filterMax'})) document.querySelector('#'+id).value=state.filters[key];
    renderBackupStatus(backups); document.querySelector('#filtersDialog').showModal();
  }catch(error){ toast(error.message); }
}
function renderBackupStatus(data){
  const latest=data.backups?.[0]; document.querySelector('#backupStatus').textContent=latest?`Последняя: ${fullDate.format(new Date(latest.created))} · ${size(latest.size)}`:'Копий пока нет';
}
async function backupNow(){
  const button=document.querySelector('#backupNow'); button.disabled=true; button.textContent='Создаём…';
  try{ await api('/api/backups',{method:'POST',body:'{}'}); renderBackupStatus(await api('/api/backups')); toast('Резервная копия создана'); }
  catch(error){ toast(error.message); } finally{ button.disabled=false; button.textContent='Создать сейчас'; }
}
function applyFilters(event){
  event.preventDefault();
  state.filters={date_from:document.querySelector('#filterDateFrom').value,date_to:document.querySelector('#filterDateTo').value,camera:document.querySelector('#filterCamera').value,gps:document.querySelector('#filterGps').value,min_mb:document.querySelector('#filterMin').value,max_mb:document.querySelector('#filterMax').value};
  document.querySelector('#filtersDialog').close(); state.onThisDay=false; renderCollectionState(); load(true);
}
function resetFilters(){
  state.filters={date_from:'',date_to:'',camera:'',gps:'',min_mb:'',max_mb:''};
  document.querySelector('#filtersForm').reset(); renderCollectionState(); load(true);
}
async function openDuplicates(mode='exact'){
  const dialog=document.querySelector('#duplicatesDialog'); if(!dialog.open) dialog.showModal();
  document.querySelectorAll('[data-duplicate-mode]').forEach(button=>button.classList.toggle('active',button.dataset.duplicateMode===mode));
  const root=document.querySelector('#duplicateList'); root.innerHTML='<div class="album-empty">Ищем совпадения…</div>';
  try{
    const data=await api(`/api/duplicates?mode=${mode}`); root.innerHTML='';
    if(data.index?.ready<data.index?.total){ const progress=document.createElement('div'); progress.className='index-progress'; progress.textContent=`Индекс похожести создаётся в фоне: ${data.index.ready.toLocaleString('ru-RU')} из ${data.index.total.toLocaleString('ru-RU')}`; root.append(progress); }
    if(!data.groups.length){ root.insertAdjacentHTML('beforeend','<div class="album-empty">Совпадений пока не найдено.</div>'); return; }
    for(const [index,group] of data.groups.entries()){
      const article=document.createElement('article'); article.className='duplicate-group';
      article.innerHTML=`<header><strong>Группа ${index+1}</strong><span>${group.count} файла · ${size(group.bytes)}</span></header><div class="duplicate-thumbs">${group.items.map(item=>`<figure><img src="${item.thumbUrl}" alt="${escapeHtml(item.name)}"><figcaption>${tileDate.format(new Date(item.taken))}<small>${escapeHtml(item.name)}</small></figcaption></figure>`).join('')}</div>`;
      root.append(article);
    }
  }catch(error){ root.innerHTML=`<div class="album-empty">${escapeHtml(error.message)}</div>`; }
}

document.querySelectorAll('.tab').forEach(tab=>tab.addEventListener('click',()=>{
  document.querySelector('.tab.active').classList.remove('active'); tab.classList.add('active'); state.type=tab.dataset.type; load(true);
}));
document.querySelector('#searchButton').addEventListener('click',()=>{ document.querySelector('#searchWrap').classList.add('open'); document.querySelector('#searchInput').focus(); });
document.querySelector('#searchInput').addEventListener('input',debounce(event=>{ state.query=event.target.value; load(true); }));
document.querySelector('#favoritesButton').addEventListener('click',()=>{ state.favoritesOnly=!state.favoritesOnly; state.albumId=null; state.albumName=''; state.onThisDay=false; renderCollectionState(); load(true); });
document.querySelector('#albumsButton').addEventListener('click',async()=>{ try{ await fetchAlbums(); document.querySelector('#albumsDialog').showModal(); }catch(error){toast(error.message);} });
document.querySelector('#mapButton').addEventListener('click',openMap);
document.querySelector('#todayButton').addEventListener('click',()=>{ state.onThisDay=!state.onThisDay; state.year=null; state.month=null; state.albumId=null; state.albumName=''; state.favoritesOnly=false; renderTimeline(); renderCollectionState(); load(true); });
document.querySelector('#duplicatesButton').addEventListener('click',()=>openDuplicates('exact'));
document.querySelectorAll('[data-duplicate-mode]').forEach(button=>button.addEventListener('click',()=>openDuplicates(button.dataset.duplicateMode)));
document.querySelector('#filtersButton').addEventListener('click',openFilters);
document.querySelector('#filtersForm').addEventListener('submit',applyFilters);
document.querySelector('#resetFilters').addEventListener('click',resetFilters);
document.querySelector('#backupNow').addEventListener('click',backupNow);
document.querySelector('#selectButton').addEventListener('click',()=>setSelecting(!state.selecting));
document.querySelector('#selectCancel').addEventListener('click',()=>setSelecting(false));
document.querySelector('#selectFavorite').addEventListener('click',bulkFavorite);
document.querySelector('#selectAlbum').addEventListener('click',async()=>{ try{ await fetchAlbums(); openAlbumPicker([...state.selected]); }catch(error){toast(error.message);} });
document.querySelector('#selectDownload').addEventListener('click',downloadSelection);
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
