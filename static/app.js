const state = { items: [], total: 0, offset: 0, type: 'all', query: '', year: null, month: null, timeline: [], loading: false, hasMore: true, favoritesOnly: false, current: -1 };
const gallery = document.querySelector('#gallery');
const sentinel = document.querySelector('#sentinel');
const viewer = document.querySelector('#viewer');
const favorites = new Set(JSON.parse(localStorage.getItem('photo-favorites') || '[]'));
const months = new Intl.DateTimeFormat('ru-RU', {month:'long', year:'numeric'});
const monthNames = Array.from({length:12},(_,i)=>new Intl.DateTimeFormat('ru-RU',{month:'short'}).format(new Date(2020,i,1)).replace('.',''));
const fullDate = new Intl.DateTimeFormat('ru-RU', {day:'numeric', month:'long', year:'numeric', hour:'2-digit', minute:'2-digit'});
const tileDate = new Intl.DateTimeFormat('ru-RU', {day:'numeric', month:'short', year:'numeric'});
const size = n => n < 1048576 ? `${Math.round(n/1024)} КБ` : `${(n/1048576).toFixed(1)} МБ`;

function saveFavorites(){ localStorage.setItem('photo-favorites', JSON.stringify([...favorites])); }
function visibleItems(){ return state.favoritesOnly ? state.items.filter(x => favorites.has(x.id)) : state.items; }
function debounce(fn, wait=300){ let timer; return (...args) => { clearTimeout(timer); timer=setTimeout(()=>fn(...args),wait); }; }

async function load(reset=false){
  if(state.loading || (!reset && !state.hasMore)) return;
  if(reset){ state.items=[]; state.offset=0; state.hasMore=true; gallery.innerHTML=''; }
  state.loading=true; sentinel.hidden=false;
  const params = new URLSearchParams({offset:state.offset,limit:60,type:state.type,q:state.query});
  if(state.year) params.set('year',state.year); if(state.month) params.set('month',state.month);
  try {
    const response = await fetch('/api/photos?'+params);
    if(!response.ok) throw new Error('Ошибка загрузки');
    const data = await response.json();
    state.items.push(...data.items); state.total=data.total; state.offset+=data.items.length; state.hasMore=data.hasMore;
    render();
  } catch(error){ gallery.innerHTML=`<div class="empty"><h2>Не удалось загрузить фотографии</h2><p>${error.message}</p></div>`; }
  finally { state.loading=false; sentinel.hidden=!state.hasMore; }
}

function render(){
  gallery.innerHTML=''; let lastMonth=''; const list=visibleItems();
  for(const item of list){
    if(item.month!==lastMonth){
      const title=document.createElement('div'); title.className='month-title';
      title.innerHTML=`${months.format(new Date(item.taken))}<span>${item.month}</span>`; gallery.append(title); lastMonth=item.month;
    }
    const tile=document.createElement('figure'); tile.className='tile'; tile.dataset.id=item.id;
    const visual=item.type==='image' ? `<img loading="lazy" src="${item.thumbUrl}" alt="${escapeHtml(item.name)}">` : `<video muted preload="metadata" src="${item.mediaUrl}#t=0.1"></video><span class="video-badge">▶</span>`;
    tile.innerHTML=`${visual}${favorites.has(item.id)?'<span class="favorite-badge">♥</span>':''}<time class="tile-date" datetime="${item.taken}">${tileDate.format(new Date(item.taken))}</time><figcaption class="tile-overlay"><span class="tile-name">${escapeHtml(item.name)}</span><span>${size(item.size)}</span></figcaption>`;
    tile.addEventListener('click',()=>openViewer(state.items.findIndex(x=>x.id===item.id)));
    tile.querySelector('img')?.addEventListener('load',e=>e.currentTarget.classList.add('loaded'));
    gallery.append(tile);
  }
  document.querySelector('#empty').hidden=list.length>0;
  document.querySelector('#resultCount').textContent=state.favoritesOnly ? `${list.length} в избранном` : `${state.total.toLocaleString('ru-RU')} файлов`;
}

function escapeHtml(value){ const div=document.createElement('div'); div.textContent=value; return div.innerHTML; }
function renderTimeline(){
  const root=document.querySelector('#timelineYears'); root.innerHTML='';
  document.querySelector('#timelineAll').classList.toggle('active',!state.year);
  for(const entry of state.timeline){
    const button=document.createElement('button'); button.className='timeline-year'+(state.year===entry.year?' active':'');
    button.textContent=entry.year; button.title=`${entry.year}: ${entry.count.toLocaleString('ru-RU')} файлов`;
    button.addEventListener('click',()=>selectYear(entry,button)); root.append(button);
  }
  const chip=document.querySelector('#periodChip'); chip.hidden=!state.year;
  if(state.year) document.querySelector('#periodLabel').textContent=state.month ? `${monthNames[state.month-1]} ${state.year}` : `${state.year} год`;
}
function selectYear(entry,anchor){
  state.year=entry.year; state.month=null; state.favoritesOnly=false; showMonths(entry,anchor); renderTimeline(); load(true);
}
function showMonths(entry,anchor){
  const picker=document.querySelector('#monthPicker');
  picker.innerHTML=`<div class="month-picker-header"><strong>${entry.year}</strong><button aria-label="Закрыть">×</button></div><div class="month-grid"></div>`;
  const counts=new Map(entry.months.map(x=>[x.month,x.count])); const grid=picker.querySelector('.month-grid');
  for(let number=1;number<=12;number++){
    if(!counts.has(number)) continue; const button=document.createElement('button'); button.className='month-button'+(state.month===number?' active':'');
    button.innerHTML=`${monthNames[number-1]}<small>${counts.get(number)}</small>`;
    button.addEventListener('click',()=>{state.year=entry.year;state.month=number;picker.hidden=true;renderTimeline();load(true);}); grid.append(button);
  }
  picker.querySelector('.month-picker-header button').addEventListener('click',()=>picker.hidden=true);
  picker.hidden=false; const rect=anchor.getBoundingClientRect(); picker.style.top=`${Math.min(innerHeight-picker.offsetHeight-12,Math.max(74,rect.top-30))}px`;
}
function clearPeriod(){ state.year=null;state.month=null;document.querySelector('#monthPicker').hidden=true;renderTimeline();load(true); }
function openViewer(index){
  if(index<0 || index>=state.items.length) return; state.current=index; const item=state.items[index];
  document.querySelector('#viewerName').textContent=item.name;
  document.querySelector('#viewerMeta').textContent=`${fullDate.format(new Date(item.taken))} · ${size(item.size)}`;
  document.querySelector('#downloadCurrent').href='/download/'+item.path.split('/').map(encodeURIComponent).join('/');
  document.querySelector('#favoriteCurrent').textContent=favorites.has(item.id)?'♥':'♡';
  document.querySelector('#viewerStage').innerHTML=item.type==='image' ? `<img src="${item.mediaUrl}" alt="${escapeHtml(item.name)}">` : `<video src="${item.mediaUrl}" controls autoplay playsinline></video>`;
  if(!viewer.open) viewer.showModal();
}
function move(direction){ const next=state.current+direction; if(next>=0 && next<state.items.length) openViewer(next); else if(next>=state.items.length && state.hasMore){ load().then(()=>openViewer(next)); } }
function toggleFavorite(){ const item=state.items[state.current]; if(!item)return; favorites.has(item.id)?favorites.delete(item.id):favorites.add(item.id); saveFavorites(); openViewer(state.current); render(); }

document.querySelectorAll('.tab').forEach(tab=>tab.addEventListener('click',()=>{document.querySelector('.tab.active').classList.remove('active');tab.classList.add('active');state.type=tab.dataset.type;state.favoritesOnly=false;load(true);}));
document.querySelector('#searchButton').addEventListener('click',()=>{document.querySelector('#searchWrap').classList.add('open');document.querySelector('#searchInput').focus();});
document.querySelector('#searchInput').addEventListener('input',debounce(e=>{state.query=e.target.value;state.favoritesOnly=false;load(true);}));
document.querySelector('#favoritesButton').addEventListener('click',e=>{state.favoritesOnly=!state.favoritesOnly;e.currentTarget.textContent=state.favoritesOnly?'♥':'♡';render();});
document.querySelector('#timelineAll').addEventListener('click',clearPeriod);
document.querySelector('#periodChip').addEventListener('click',clearPeriod);
document.querySelector('#closeViewer').addEventListener('click',()=>viewer.close());
document.querySelector('#prevItem').addEventListener('click',()=>move(-1)); document.querySelector('#nextItem').addEventListener('click',()=>move(1));
document.querySelector('#favoriteCurrent').addEventListener('click',toggleFavorite);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.querySelector('#searchWrap').classList.contains('open')&&!viewer.open){document.querySelector('#searchWrap').classList.remove('open');}if(viewer.open&&e.key==='ArrowLeft')move(-1);if(viewer.open&&e.key==='ArrowRight')move(1);});
viewer.addEventListener('click',e=>{if(e.target===viewer) viewer.close();});
new IntersectionObserver(entries=>{if(entries[0].isIntersecting)load();},{rootMargin:'600px'}).observe(sentinel);
fetch('/api/stats').then(r=>r.json()).then(s=>{document.querySelector('#summary').textContent=`${s.images.toLocaleString('ru-RU')} фотографий · ${s.videos.toLocaleString('ru-RU')} видео · только на вашем сервере`;});
fetch('/api/timeline').then(r=>r.json()).then(data=>{state.timeline=data.years;renderTimeline();});
load();
