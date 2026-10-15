const state = {
  items: [], total: 0, offset: 0, type: 'all', query: '', year: null, month: null, undated: false,
  timeline: [], undatedCount: 0, albums: [], albumId: null, albumName: '', loading: false, hasMore: true,
  favoritesOnly: false, onThisDay: false, current: -1, controller: null, map: null, mapLayer: null,
  smartFilter: '', smartName: '', smartTimer: null,
  people: [], personIds: [], personNames: [], personMode: 'all', activePerson: null,
  peopleDraftIds: new Set(), peopleDraftMode: 'all',
  filters: {date_from:'',date_to:'',camera:'',gps:'',min_mb:'',max_mb:''},
  selecting: false, selected: new Set(), albumTargetIds: [], user: null, csrf: ''
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
const roleLevel={viewer:1,editor:2,admin:3};
const roleNames={viewer:'Просмотр',editor:'Редактор',admin:'Администратор'};
function can(role){ return roleLevel[state.user?.role||'viewer']>=roleLevel[role]; }
function resetPeopleFilter(){ state.personIds=[]; state.personNames=[]; state.personMode='all'; }
function peopleFilterLabel(){
  if(!state.personNames.length) return '';
  if(state.personNames.length===1) return state.personNames[0];
  const separator=state.personMode==='all'?' + ':' или ';
  if(state.personNames.length<=3) return state.personNames.join(separator);
  return `${state.personNames.slice(0,2).join(separator)} · ещё ${state.personNames.length-2}`;
}
function applyPermissions(){
  document.querySelectorAll('[data-min-role]').forEach(element=>element.hidden=!can(element.dataset.minRole));
  const button=document.querySelector('#accountButton'); button.textContent=(state.user?.username||'?').slice(0,1).toUpperCase(); button.title=state.user?.username||'Аккаунт';
}

async function api(url, options={}){
  const method=(options.method||'GET').toUpperCase(); const headers={'Content-Type':'application/json',...(options.headers||{})};
  if(method!=='GET'&&method!=='HEAD'&&state.csrf) headers['X-CSRF-Token']=state.csrf;
  const response=await fetch(url,{...options,headers});
  if(response.status===401){ location.replace('/login'); throw new Error('Сессия завершена'); }
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
  if(state.undated) params.set('undated','1');
  if(state.favoritesOnly) params.set('favorite','1');
  if(state.albumId) params.set('album',state.albumId);
  if(state.smartFilter) params.set('smart',state.smartFilter);
  for(const personId of state.personIds) params.append('person',personId);
  if(state.personIds.length) params.set('people_mode',state.personMode);
  if(state.onThisDay){ const now=new Date(); params.set('month',now.getMonth()+1); params.set('day',now.getDate()); params.set('before_year',now.getFullYear()-1); }
  for(const [key,value] of Object.entries(state.filters)) if(value!=='') params.set(key,value);
  try{
    const response=await fetch('/api/photos?'+params,{signal:controller.signal});
    if(response.status===401){ location.replace('/login'); return; }
    if(!response.ok) throw new Error('Ошибка загрузки');
    const data=await response.json();
    const renderFrom=state.items.length;
    state.items.push(...data.items); state.total=data.total; state.offset+=data.items.length; state.hasMore=data.hasMore;
    render(renderFrom);
  }catch(error){
    if(error.name!=='AbortError'){
      if(state.items.length) toast('Не удалось загрузить следующую часть галереи');
      else gallery.innerHTML=`<div class="empty"><h2>Не удалось загрузить фотографии</h2><p>${escapeHtml(error.message)}</p></div>`;
    }
  }finally{
    if(state.controller===controller){ state.loading=false; sentinel.hidden=!state.hasMore; }
  }
}

function render(from=0){
  from=Math.max(0,Math.min(from,state.items.length));
  if(from===0) gallery.innerHTML='';
  const fragment=document.createDocumentFragment();
  const previous=from?state.items[from-1]:null;
  let lastMonth=previous?(previous.dateSource==='modified'?'undated':previous.month):'';
  for(let index=from;index<state.items.length;index++){
    const item=state.items[index];
    const isUndated=item.dateSource==='modified'; const group=isUndated?'undated':item.month;
    if(group!==lastMonth){
      const title=document.createElement('div'); title.className='month-title';
      title.innerHTML=isUndated?'Без даты<span>дата съёмки неизвестна</span>':`${months.format(new Date(item.taken))}<span>${item.month}</span>`; fragment.append(title); lastMonth=group;
    }
    const tile=document.createElement('figure'); tile.className='tile'+(state.selected.has(item.id)?' selected':''); tile.dataset.id=item.id;
    const visual=item.type==='image'
      ? `<img loading="lazy" src="${item.thumbUrl}" alt="${escapeHtml(item.name)}">`
      : `<video muted preload="metadata" src="${item.mediaUrl}#t=0.1"></video><span class="video-badge">▶</span>`;
    const dateBadge=isUndated?'<span class="tile-date">Без даты</span>':`<time class="tile-date" datetime="${item.taken}">${tileDate.format(new Date(item.taken))}</time>`;
    tile.innerHTML=`${visual}<span class="selection-check">${state.selected.has(item.id)?'✓':''}</span>${item.favorite?'<span class="favorite-badge">♥</span>':''}${dateBadge}<figcaption class="tile-overlay"><span class="tile-name">${escapeHtml(item.name)}</span><span>${size(item.size)}</span></figcaption>`;
    tile.addEventListener('click',()=>state.selecting?toggleSelection(item.id):openViewer(state.items.findIndex(value=>value.id===item.id)));
    const image=tile.querySelector('img');
    if(image){ if(image.complete) image.classList.add('loaded'); else image.addEventListener('load',event=>event.currentTarget.classList.add('loaded'),{once:true}); }
    fragment.append(tile);
  }
  gallery.append(fragment);
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
  chip.hidden=!state.favoritesOnly&&!state.albumId&&!state.onThisDay&&!state.smartFilter&&!state.personIds.length&&!activeFilters;
  document.querySelector('#collectionLabel').textContent=state.personIds.length?peopleFilterLabel():state.smartFilter?state.smartName:state.onThisDay?'В этот день':state.favoritesOnly?'Избранное':state.albumId?state.albumName:`Фильтры: ${activeFilters}`;
  document.querySelector('#todayButton').classList.toggle('active',state.onThisDay);
  document.querySelector('#smartButton').classList.toggle('active',Boolean(state.smartFilter));
  document.querySelector('#filtersButton').classList.toggle('active',Boolean(activeFilters));
}

function renderTimeline(){
  const root=document.querySelector('#timelineYears'); root.innerHTML='';
  document.querySelector('#timelineAll').classList.toggle('active',!state.year&&!state.undated);
  for(const entry of state.timeline){
    const button=document.createElement('button'); button.className='timeline-year'+(state.year===entry.year?' active':'');
    button.textContent=entry.year; button.title=`${entry.year}: ${entry.count.toLocaleString('ru-RU')} файлов`;
    button.addEventListener('click',()=>selectYear(entry,button)); root.append(button);
  }
  const undated=document.querySelector('#timelineUndated'); undated.hidden=!state.undatedCount; undated.classList.toggle('active',state.undated); undated.title=`Без даты: ${state.undatedCount.toLocaleString('ru-RU')} файлов`;
  const chip=document.querySelector('#periodChip'); chip.hidden=!state.year&&!state.undated;
  if(state.undated) document.querySelector('#periodLabel').textContent='Без даты';
  else if(state.year) document.querySelector('#periodLabel').textContent=state.month?`${monthNames[state.month-1]} ${state.year}`:`${state.year} год`;
}

function selectYear(entry,anchor){ state.year=entry.year; state.month=null; state.undated=false; state.onThisDay=false; showMonths(entry,anchor); renderTimeline(); renderCollectionState(); load(true); }
function selectUndated(){ state.year=null; state.month=null; state.undated=true; state.onThisDay=false; document.querySelector('#monthPicker').hidden=true; renderTimeline(); renderCollectionState(); load(true); }
function showMonths(entry,anchor){
  const picker=document.querySelector('#monthPicker');
  picker.innerHTML=`<div class="month-picker-header"><strong>${entry.year}</strong><button aria-label="Закрыть">×</button></div><div class="month-grid"></div>`;
  const counts=new Map(entry.months.map(value=>[value.month,value.count])); const grid=picker.querySelector('.month-grid');
  for(let number=1;number<=12;number++){
    if(!counts.has(number)) continue;
    const button=document.createElement('button'); button.className='month-button'+(state.month===number?' active':'');
    button.innerHTML=`${monthNames[number-1]}<small>${counts.get(number)}</small>`;
    button.addEventListener('click',()=>{ state.year=entry.year; state.month=number; state.undated=false; picker.hidden=true; renderTimeline(); load(true); }); grid.append(button);
  }
  picker.querySelector('.month-picker-header button').addEventListener('click',()=>picker.hidden=true);
  picker.hidden=false; const rect=anchor.getBoundingClientRect(); picker.style.top=`${Math.min(innerHeight-picker.offsetHeight-12,Math.max(74,rect.top-30))}px`;
}
function clearPeriod(){ state.year=null; state.month=null; state.undated=false; state.onThisDay=false; document.querySelector('#monthPicker').hidden=true; renderTimeline(); renderCollectionState(); load(true); }
function clearCollection(){ state.favoritesOnly=false; state.albumId=null; state.albumName=''; state.onThisDay=false; state.smartFilter=''; state.smartName=''; resetPeopleFilter(); state.filters={date_from:'',date_to:'',camera:'',gps:'',min_mb:'',max_mb:''}; renderCollectionState(); load(true); }

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
  const metadata=[item.dateSource==='modified'?'Дата съёмки неизвестна':fullDate.format(new Date(item.taken)),size(item.size)];
  if(item.width&&item.height) metadata.push(`${item.width}×${item.height}`);
  if(item.cameraModel) metadata.push(item.cameraModel);
  if(item.latitude!=null) metadata.push('есть геопозиция');
  if(item.faceCount) metadata.push(`лиц: ${item.faceCount}`);
  document.querySelector('#viewerMeta').textContent=metadata.join(' · ');
  document.querySelector('#downloadCurrent').href='/download/'+item.path.split('/').map(encodeURIComponent).join('/');
  document.querySelector('#favoriteCurrent').textContent=item.favorite?'♥':'♡';
  document.querySelector('#albumCurrent').classList.toggle('active',item.albumIds.length>0);
  stopViewerMedia();
  document.querySelector('#viewerStage').innerHTML=item.type==='image'
    ? `<img src="${item.mediaUrl}" alt="${escapeHtml(item.name)}">`
    : `<video src="${item.mediaUrl}" controls autoplay playsinline></video>`;
  if(!viewer.open) viewer.showModal();
}

function stopViewerMedia(){
  const stage=document.querySelector('#viewerStage'); const video=stage.querySelector('video');
  if(video){ video.pause(); video.removeAttribute('src'); video.load(); }
  stage.replaceChildren();
}
function closeViewer(){ stopViewerMedia(); if(viewer.open) viewer.close(); }

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
  if(!can('editor')) return;
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
    card.innerHTML=`${cover}<div><strong>${escapeHtml(album.name)}</strong><small>${album.count.toLocaleString('ru-RU')} фото</small></div>${can('editor')?'<button class="album-delete" title="Удалить альбом" aria-label="Удалить альбом">×</button>':''}`;
    card.addEventListener('click',()=>selectAlbum(album));
    card.querySelector('.album-delete')?.addEventListener('click',async event=>{
      event.stopPropagation(); if(!confirm(`Удалить альбом «${album.name}»? Фотографии останутся в архиве.`)) return;
      try{ await api(`/api/albums/${album.id}`,{method:'DELETE'}); if(state.albumId===album.id) clearCollection(); await fetchAlbums(); toast('Альбом удалён'); }catch(error){ toast(error.message); }
    });
    root.append(card);
  }
}

function selectAlbum(album){
  state.albumId=album.id; state.albumName=album.name; state.favoritesOnly=false; state.onThisDay=false; state.smartFilter=''; state.smartName=''; resetPeopleFilter();
  document.querySelector('#albumsDialog').close(); renderCollectionState(); load(true);
}

async function openSmartCatalog(){
  const dialog=document.querySelector('#smartDialog'); if(!dialog.open) dialog.showModal();
  try{
    const data=await api('/api/smart/catalog'); renderSmartCatalog(data);
    clearTimeout(state.smartTimer);
    if(data.progress.ready<data.progress.total) state.smartTimer=setTimeout(()=>{ if(dialog.open) openSmartCatalog(); },10000);
  }catch(error){ document.querySelector('#smartProgress').textContent=error.message; }
}

function renderSmartCatalog(data){
  const progress=data.progress; const percent=progress.total?Math.round(progress.ready/progress.total*100):100;
  const status=progress.ready>=progress.total
    ? `<strong>Каталог готов</strong> · обработано ${progress.ready.toLocaleString('ru-RU')} фотографий`
    : `<strong>${percent}%</strong> · обработано ${progress.ready.toLocaleString('ru-RU')} из ${progress.total.toLocaleString('ru-RU')} · в очереди ${progress.pending.toLocaleString('ru-RU')}`;
  const warning=progress.failed?` · ошибок: ${progress.failed.toLocaleString('ru-RU')}`:'';
  const ocr=progress.ocrAvailable?'':' · распознавание текста недоступно';
  document.querySelector('#smartProgress').innerHTML=status+warning+ocr;
  const root=document.querySelector('#smartCollections'); root.innerHTML='';
  const faces=data.faces||{}; const people=document.createElement('button'); people.className='smart-card';
  people.innerHTML=`<span class="smart-card-icon">☺</span><span class="smart-card-count">${(faces.people||0).toLocaleString('ru-RU')}</span><strong>Люди</strong><small>${faces.ready>=faces.total?'Группы лиц готовы':`Обработано ${faces.ready||0} из ${faces.total||0} фотографий`}</small>`;
  people.addEventListener('click',openPeople); root.append(people);
  for(const collection of data.collections){
    const button=document.createElement('button'); button.className='smart-card'+(collection.coverUrl?' has-cover':''); button.disabled=!collection.count;
    if(collection.coverUrl) button.style.backgroundImage=`url("${collection.coverUrl.replaceAll('"','%22')}")`;
    button.innerHTML=`<span class="smart-card-icon">${escapeHtml(collection.icon)}</span><span class="smart-card-count">${collection.count.toLocaleString('ru-RU')}</span><strong>${escapeHtml(collection.name)}</strong><small>${escapeHtml(collection.description)}</small>`;
    button.addEventListener('click',()=>selectSmartCollection(collection)); root.append(button);
  }
}

function selectSmartCollection(collection){
  state.smartFilter=collection.key; state.smartName=collection.name; state.favoritesOnly=false; state.albumId=null; state.albumName=''; state.onThisDay=false;
  resetPeopleFilter(); state.year=null; state.month=null; state.undated=false; document.querySelector('#smartDialog').close(); renderTimeline(); renderCollectionState(); load(true);
}

async function openPeople(){
  if(document.querySelector('#smartDialog').open) document.querySelector('#smartDialog').close();
  const dialog=document.querySelector('#peopleDialog'); if(!dialog.open) dialog.showModal();
  state.peopleDraftIds=new Set(state.personIds); state.peopleDraftMode=state.personMode; renderPeopleSelection();
  const root=document.querySelector('#peopleList'); root.innerHTML='<div class="album-empty">Загружаем людей…</div>';
  try{
    const data=await api('/api/people'); state.people=data.people; const progress=data.progress;
    document.querySelector('#peopleProgress').textContent=progress.ready>=progress.total
      ? `${progress.faces.toLocaleString('ru-RU')} лиц · ${progress.people.toLocaleString('ru-RU')} групп`
      : `Обработано ${progress.ready.toLocaleString('ru-RU')} из ${progress.total.toLocaleString('ru-RU')} фотографий · найдено лиц: ${progress.faces.toLocaleString('ru-RU')}`;
    const availableIds=new Set(state.people.map(person=>person.id)); state.peopleDraftIds=new Set([...state.peopleDraftIds].filter(id=>availableIds.has(id))); root.innerHTML='';
    if(!state.people.length){ root.innerHTML='<div class="album-empty">Лица пока не найдены. Фоновая обработка продолжается.</div>'; return; }
    for(const person of state.people){
      const selected=state.peopleDraftIds.has(person.id); const card=document.createElement('article'); card.className='person-card'+(selected?' selected':'');
      card.innerHTML=`<button aria-pressed="${selected}"><img loading="lazy" src="${person.coverUrl}" alt=""><span class="person-check">✓</span><span class="person-group-id">#${person.id}</span><span class="person-card-info"><strong>${escapeHtml(person.name)}</strong><small>${person.photoCount.toLocaleString('ru-RU')} фото · ${person.faceCount.toLocaleString('ru-RU')} лиц</small></span></button>${can('editor')?'<button class="person-review" title="Проверить группу">⋯</button>':''}`;
      card.querySelector('button:first-child').addEventListener('click',()=>togglePersonSelection(person,card));
      card.querySelector('.person-review')?.addEventListener('click',()=>openPersonFaces(person)); root.append(card);
    }
    renderPeopleSelection();
  }catch(error){ root.innerHTML=`<div class="album-empty">${escapeHtml(error.message)}</div>`; }
}

function togglePersonSelection(person,card){
  if(state.peopleDraftIds.has(person.id)) state.peopleDraftIds.delete(person.id);
  else if(state.peopleDraftIds.size>=20){ toast('Можно выбрать не более 20 человек'); return; }
  else state.peopleDraftIds.add(person.id);
  const selected=state.peopleDraftIds.has(person.id); card.classList.toggle('selected',selected); card.querySelector('button:first-child').setAttribute('aria-pressed',selected); renderPeopleSelection();
}

function renderPeopleSelection(){
  document.querySelectorAll('[data-people-mode]').forEach(button=>button.classList.toggle('active',button.dataset.peopleMode===state.peopleDraftMode));
  const count=state.peopleDraftIds.size; document.querySelector('#peopleSelectionCount').textContent=count?`Выбрано: ${count}`:'Никто не выбран'; document.querySelector('#peopleApply').disabled=!count;
}

function applyPeopleFilter(){
  const selected=state.people.filter(person=>state.peopleDraftIds.has(person.id)); if(!selected.length) return;
  state.personIds=selected.map(person=>person.id); state.personNames=selected.map(person=>person.name); state.personMode=state.peopleDraftMode;
  state.smartFilter=''; state.smartName=''; state.favoritesOnly=false; state.albumId=null; state.albumName=''; state.onThisDay=false;
  state.year=null; state.month=null; state.undated=false; document.querySelector('#peopleDialog').close(); renderTimeline(); renderCollectionState(); load(true);
}

async function openPersonFaces(person){
  state.activePerson=person; document.querySelector('#personFacesTitle').textContent=person.name;
  const dialog=document.querySelector('#personFacesDialog'); if(!dialog.open) dialog.showModal();
  const root=document.querySelector('#personFacesList'); root.innerHTML='<div class="album-empty">Загружаем лица…</div>';
  try{
    const data=await api(`/api/people/${person.id}/faces`); root.innerHTML='';
    for(const face of data.faces){
      const card=document.createElement('figure'); card.className='face-card';
      card.innerHTML=`<img loading="lazy" src="${face.thumbUrl}" alt=""><figcaption>${tileDate.format(new Date(face.taken))}${can('editor')?'<button class="face-move">Перенести…</button>':''}</figcaption>`;
      card.querySelector('.face-move')?.addEventListener('click',()=>moveFace(face)); root.append(card);
    }
  }catch(error){ root.innerHTML=`<div class="album-empty">${escapeHtml(error.message)}</div>`; }
}

async function renameActivePerson(){
  const person=state.activePerson; if(!person) return; const name=prompt('Имя человека',person.named?person.name:''); if(name===null||!name.trim()) return;
  try{ const updated=await api(`/api/people/${person.id}`,{method:'POST',body:JSON.stringify({name})}); state.activePerson=updated; document.querySelector('#personFacesTitle').textContent=updated.name; toast('Имя сохранено'); await openPeople(); }
  catch(error){ toast(error.message); }
}

async function mergeActivePerson(){
  const source=state.activePerson; if(!source) return; const answer=prompt('Введите имя или номер группы, с которой объединить'); if(answer===null||!answer.trim()) return;
  const needle=answer.trim().toLocaleLowerCase('ru-RU'); const target=state.people.find(person=>String(person.id)===needle||person.name.toLocaleLowerCase('ru-RU')===needle);
  if(!target||target.id===source.id){ toast('Подходящая другая группа не найдена'); return; }
  if(!confirm(`Объединить «${source.name}» с «${target.name}»?`)) return;
  try{ await api('/api/people/merge',{method:'POST',body:JSON.stringify({sourceId:source.id,targetId:target.id})}); document.querySelector('#personFacesDialog').close(); toast('Группы объединены'); await openPeople(); }
  catch(error){ toast(error.message); }
}

async function moveFace(face){
  const answer=prompt('Введите имя или номер другой группы. Оставьте пустым, чтобы создать новую.'); if(answer===null) return;
  let target=null; if(answer.trim()){ const needle=answer.trim().toLocaleLowerCase('ru-RU'); target=state.people.find(person=>String(person.id)===needle||person.name.toLocaleLowerCase('ru-RU')===needle); if(!target){ toast('Группа не найдена'); return; } }
  try{ await api(`/api/faces/${face.id}`,{method:'POST',body:JSON.stringify({personId:target?.id??null})}); toast(target?'Лицо перенесено':'Создана новая группа'); await openPersonFaces(state.activePerson); }
  catch(error){ toast(error.message); }
}

async function reindexFaces(){
  if(!confirm('Заново найти и сгруппировать все лица? Имена и ручные исправления будут удалены.')) return;
  try{ const data=await api('/api/faces/reindex',{method:'POST',body:'{}'}); toast(`В очередь добавлено: ${data.queued}`); await openPeople(); }
  catch(error){ toast(error.message); }
}

async function reindexSmartCatalog(){
  if(!confirm('Переобработать все фотографии? Существующие результаты будут построены заново в фоне.')) return;
  const button=document.querySelector('#smartReindex'); button.disabled=true;
  try{ const result=await api('/api/smart/reindex',{method:'POST',body:'{}'}); toast(`В очередь добавлено: ${result.queued}`); await openSmartCatalog(); }
  catch(error){ toast(error.message); } finally{ button.disabled=false; }
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
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{
        maxZoom:19,
        attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>'
      }).addTo(state.map);
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
    const [options,backups]=await Promise.all([api('/api/filters'),can('admin')?api('/api/backups'):Promise.resolve({backups:[]})]);
    const camera=document.querySelector('#filterCamera'); camera.innerHTML='<option value="">Любая камера</option>';
    for(const entry of options.cameras){ const option=document.createElement('option'); option.value=entry.value; option.textContent=`${entry.value} · ${entry.count}`; camera.append(option); }
    for(const [key,id] of Object.entries({date_from:'filterDateFrom',date_to:'filterDateTo',camera:'filterCamera',gps:'filterGps',min_mb:'filterMin',max_mb:'filterMax'})) document.querySelector('#'+id).value=state.filters[key];
    if(can('admin')) renderBackupStatus(backups); document.querySelector('#filtersDialog').showModal();
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

async function openAccount(){
  document.querySelector('#accountTitle').textContent=state.user.username;
  document.querySelector('#accountRole').textContent=roleNames[state.user.role];
  const dialog=document.querySelector('#accountDialog'); if(!dialog.open) dialog.showModal();
  if(can('admin')){ try{ renderUsers((await api('/api/users')).users); }catch(error){ toast(error.message); } }
}
function renderUsers(users){
  const root=document.querySelector('#userList'); root.innerHTML='';
  for(const user of users){
    const card=document.createElement('article'); card.className='user-card'; card.dataset.id=user.id;
    card.innerHTML=`<div class="user-identity"><strong>${escapeHtml(user.username)}</strong><small>${user.lastLogin?'Последний вход: '+fullDate.format(new Date(user.lastLogin)):'Ещё не входил'}</small></div><select class="user-role"><option value="viewer">Просмотр</option><option value="editor">Редактор</option><option value="admin">Администратор</option></select><label class="user-active"><input type="checkbox" ${user.active?'checked':''}> Активен</label><input class="user-password" type="password" minlength="10" maxlength="256" placeholder="Новый пароль (необязательно)" autocomplete="new-password"><button class="user-save">Сохранить</button>`;
    card.querySelector('.user-role').value=user.role;
    card.querySelector('.user-save').addEventListener('click',async()=>{
      const payload={role:card.querySelector('.user-role').value,active:card.querySelector('.user-active input').checked}; const password=card.querySelector('.user-password').value; if(password) payload.password=password;
      try{ await api(`/api/users/${user.id}`,{method:'POST',body:JSON.stringify(payload)}); toast('Пользователь обновлён'); renderUsers((await api('/api/users')).users); }
      catch(error){ toast(error.message); }
    }); root.append(card);
  }
}
async function createUser(event){
  event.preventDefault();
  const payload={username:document.querySelector('#newUsername').value,password:document.querySelector('#newUserPassword').value,role:document.querySelector('#newUserRole').value};
  try{ await api('/api/users',{method:'POST',body:JSON.stringify(payload)}); event.target.reset(); renderUsers((await api('/api/users')).users); toast('Пользователь создан'); }
  catch(error){ toast(error.message); }
}
async function changePassword(event){
  event.preventDefault();
  try{ await api('/api/account/password',{method:'POST',body:JSON.stringify({currentPassword:document.querySelector('#currentPassword').value,newPassword:document.querySelector('#newPassword').value})}); location.replace('/login'); }
  catch(error){ toast(error.message); }
}
async function logout(){
  try{ await api('/api/auth/logout',{method:'POST',body:'{}'}); }catch(_error){} location.replace('/login');
}

document.querySelectorAll('.tab').forEach(tab=>tab.addEventListener('click',()=>{
  document.querySelector('.tab.active').classList.remove('active'); tab.classList.add('active'); state.type=tab.dataset.type; load(true);
}));
document.querySelector('#searchButton').addEventListener('click',()=>{ document.querySelector('#searchWrap').classList.add('open'); document.querySelector('#searchInput').focus(); });
document.querySelector('#searchInput').addEventListener('input',debounce(event=>{ state.query=event.target.value; load(true); }));
document.querySelector('#favoritesButton').addEventListener('click',()=>{ state.favoritesOnly=!state.favoritesOnly; state.albumId=null; state.albumName=''; state.onThisDay=false; state.smartFilter=''; state.smartName=''; resetPeopleFilter(); renderCollectionState(); load(true); });
document.querySelector('#smartButton').addEventListener('click',openSmartCatalog);
document.querySelector('#smartReindex').addEventListener('click',reindexSmartCatalog);
document.querySelector('#facesReindex').addEventListener('click',reindexFaces);
document.querySelector('#renamePerson').addEventListener('click',renameActivePerson);
document.querySelector('#mergePerson').addEventListener('click',mergeActivePerson);
document.querySelectorAll('[data-people-mode]').forEach(button=>button.addEventListener('click',()=>{ state.peopleDraftMode=button.dataset.peopleMode; renderPeopleSelection(); }));
document.querySelector('#peopleApply').addEventListener('click',applyPeopleFilter);
document.querySelector('#albumsButton').addEventListener('click',async()=>{ try{ await fetchAlbums(); document.querySelector('#albumsDialog').showModal(); }catch(error){toast(error.message);} });
document.querySelector('#mapButton').addEventListener('click',openMap);
document.querySelector('#accountButton').addEventListener('click',openAccount);
document.querySelector('#todayButton').addEventListener('click',()=>{ state.onThisDay=!state.onThisDay; state.year=null; state.month=null; state.undated=false; state.albumId=null; state.albumName=''; state.favoritesOnly=false; state.smartFilter=''; state.smartName=''; resetPeopleFilter(); renderTimeline(); renderCollectionState(); load(true); });
document.querySelector('#duplicatesButton').addEventListener('click',()=>openDuplicates('exact'));
document.querySelectorAll('[data-duplicate-mode]').forEach(button=>button.addEventListener('click',()=>openDuplicates(button.dataset.duplicateMode)));
document.querySelector('#filtersButton').addEventListener('click',openFilters);
document.querySelector('#filtersForm').addEventListener('submit',applyFilters);
document.querySelector('#resetFilters').addEventListener('click',resetFilters);
document.querySelector('#backupNow').addEventListener('click',backupNow);
document.querySelector('#userForm').addEventListener('submit',createUser);
document.querySelector('#passwordForm').addEventListener('submit',changePassword);
document.querySelector('#logoutButton').addEventListener('click',logout);
document.querySelector('#selectButton').addEventListener('click',()=>setSelecting(!state.selecting));
document.querySelector('#selectCancel').addEventListener('click',()=>setSelecting(false));
document.querySelector('#selectFavorite').addEventListener('click',bulkFavorite);
document.querySelector('#selectAlbum').addEventListener('click',async()=>{ try{ await fetchAlbums(); openAlbumPicker([...state.selected]); }catch(error){toast(error.message);} });
document.querySelector('#selectDownload').addEventListener('click',downloadSelection);
document.querySelector('#albumForm').addEventListener('submit',createAlbum);
document.querySelector('#timelineAll').addEventListener('click',clearPeriod);
document.querySelector('#timelineUndated').addEventListener('click',selectUndated);
document.querySelector('#periodChip').addEventListener('click',clearPeriod);
document.querySelector('#collectionChip').addEventListener('click',clearCollection);
document.querySelector('#closeViewer').addEventListener('click',closeViewer);
document.querySelector('#prevItem').addEventListener('click',()=>move(-1));
document.querySelector('#nextItem').addEventListener('click',()=>move(1));
document.querySelector('#favoriteCurrent').addEventListener('click',toggleFavorite);
document.querySelector('#albumCurrent').addEventListener('click',openAlbumPicker);
document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>document.querySelector('#'+button.dataset.close).close()));
document.querySelector('#smartDialog').addEventListener('close',()=>clearTimeout(state.smartTimer));

document.addEventListener('keydown',event=>{
  if(event.key==='Escape'&&document.querySelector('#searchWrap').classList.contains('open')&&!viewer.open) document.querySelector('#searchWrap').classList.remove('open');
  if(viewer.open&&event.key==='ArrowLeft') move(-1);
  if(viewer.open&&event.key==='ArrowRight') move(1);
});
[viewer,...document.querySelectorAll('.panel-dialog')].forEach(dialog=>dialog.addEventListener('click',event=>{ if(event.target===dialog) dialog.close(); }));
viewer.addEventListener('close',stopViewerMedia);
new IntersectionObserver(entries=>{ if(entries[0].isIntersecting) load(); },{rootMargin:'600px'}).observe(sentinel);

async function initialize(){
  try{
    const session=await api('/api/session'); state.user=session.user; state.csrf=session.csrf; applyPermissions();
    await importBrowserFavorites();
    const [stats,timeline]=await Promise.all([api('/api/stats'),api('/api/timeline'),fetchAlbums()]);
    const summary=document.querySelector('#summary');
    summary.textContent=stats.storageAvailable
      ? `${stats.images.toLocaleString('ru-RU')} фотографий · ${stats.videos.toLocaleString('ru-RU')} видео · ${stats.located.toLocaleString('ru-RU')} на карте`
      : `Хранилище недоступно: не найден файл ${stats.storageMarker}. Индекс сохранён, фотографии не удалены.`;
    summary.classList.toggle('storage-warning',!stats.storageAvailable);
    state.timeline=timeline.years; state.undatedCount=timeline.undated||0; renderTimeline();
  }catch(error){ toast(error.message); }
  load();
}
initialize();
