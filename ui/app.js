/* FreeConnect UI — логика интерфейса.
   Работает поверх бэкенда pywebview (window.pywebview.api), а при его отсутствии
   (открытие в браузере для отладки дизайна) — поверх встроенного мока MockApi. */

/* ===== ВРЕМЕННАЯ ДИАГНОСТИКА ЗАВИСАНИЯ (убрать после локализации) =====
   Метки этапов + детектор разрывов кадров. Если контент виснет — rAF
   пропускает время, и в лог попадёт FRAME_GAP с длительностью, а рядом —
   последняя метка (какой шаг выполнялся перед зависанием). Через 16с всё
   сливается в debug.log через api().dbg_dump. */
function fmark(_m){}   // диагностика зависания снята (баг найден и исправлен)
function fflush(){}
/* ===== конец диагностики ===== */

// ---------- Слой API ----------
const MOCK_STRATEGIES = [
  "ALT","ALT2","ALT3","ALT4","ALT5","ALT6","ALT7","ALT8","ALT9","ALT10","ALT11","ALT12",
  "FAKE TLS AUTO","FAKE TLS AUTO ALT","FAKE TLS AUTO ALT2","FAKE TLS AUTO ALT3",
  "SIMPLE FAKE","SIMPLE FAKE ALT","SIMPLE FAKE ALT2","Default"
];

const MockApi = {
  _state:{enabled:false, strategy:null, working:[]},
  async get_state(){ return this._state; },
  async enable(){ this._state.enabled=true; return this._state; },
  async disable(){ this._state.enabled=false; return this._state; },
  async pick_strategy(id){ this._state.strategy=id; this._state.enabled=true; return this._state; },
  async status(){
    if(!this._state.enabled) return {discord:null,youtube:null,voice:null};
    return {discord:{ok:true,latency:142},youtube:{ok:true,latency:168},voice:{rtt:38}};
  },
  // Имитация поиска: шлём прогресс через глобальные колбэки, как это будет делать бэкенд.
  async start_search(){
    let i=0;
    const total=MOCK_STRATEGIES.length;
    const working=[];
    const step=()=>{
      if(i>=total){
        this._state.working=working;
        if(working[0]){ this._state.strategy=working[0].id; this._state.enabled=true; }
        window.onSearchDone(working);
        return;
      }
      const name=MOCK_STRATEGIES[i];
      window.onSearchProgress(i,total,name);
      // Случайно назначаем результат
      setTimeout(()=>{
        const r=Math.random();
        let d=r>.5?3:0, y=r>.35?(r>.7?3:1):0;
        const isWork = d===3 && y===3;
        if(isWork){
          const item={id:name,name,discord:3,youtube:3,latency:150+Math.round(Math.random()*40)};
          working.push(item);
          window.onSearchFound(item);
        }
        window.onSearchResult(i,total,name,{discord:d,youtube:y});
        i++; step();
      }, 260);
    };
    step();
  }
};

const api = () => (window.pywebview && window.pywebview.api) ? window.pywebview.api : MockApi;

// ---------- DOM ----------
const $ = s => document.querySelector(s);
const power=$("#power"), powerBtn=$("#powerBtn"), powerWord=$("#powerWord"), statusLine=$("#statusLine");
const currentStrategy=$("#currentStrategy");
let state={enabled:false, strategy:null, working:[]};
let busy=false;

// ---------- Рендер ----------
function setDot(id, cls){ const d=$(id); d.className="chip-dot"+(cls?" "+cls:""); }

// Плавно «перетекающее» число (пинг), а не резкий скачок
function animateNumber(el, to, fmt){
  const cur=parseInt(el.dataset.val||"",10);
  const from=Number.isFinite(cur)?cur:to;
  el.dataset.val=to;
  if(from===to){ el.textContent=fmt(to); return; }
  const start=performance.now(), dur=500;
  (function step(now){
    const t=Math.min(1,(now-start)/dur);
    const v=Math.round(from+(to-from)*(1-(1-t)*(1-t)));
    el.textContent=fmt(v);
    if(t<1) requestAnimationFrame(step);
  })(performance.now());
}

function renderPower(){
  power.classList.remove("connecting");
  if(state.enabled){
    power.classList.add("on","charged");
    powerWord.textContent="ON";
    statusLine.textContent="Обход активен";
    statusLine.classList.add("on");
  }else{
    power.classList.remove("on","charged");
    powerWord.textContent="OFF";
    statusLine.textContent="Обход выключен";
    statusLine.classList.remove("on");
  }
  currentStrategy.textContent = state.strategy || "не выбрана";
  // Заряд молнии дотягивается до 100% ровно когда обход активен (или падает до 0)
  setCharge(state.enabled ? 1 : 0, 450);
}

let statusBusy=false;
async function refreshStatus(){
  if(!state.enabled){
    setDot("#dotDiscord",""); setDot("#dotYoutube",""); setDot("#dotVoice","");
    $("#stDiscord").textContent="—"; $("#stYoutube").textContent="—"; $("#stVoice").textContent="пинг —";
    return;
  }
  if(statusBusy) return;          // защита от наложения опросов
  statusBusy=true;
  let s;
  try{ s=await api().status(); }
  catch(e){ statusBusy=false; return; }
  statusBusy=false;
  const put=(dot,txt,info)=>{
    const el=$(txt);
    if(!info){ setDot(dot,"checking"); el.textContent="проверка…"; delete el.dataset.val; return; }
    if(info.ok===false){ setDot(dot,"bad"); el.textContent="недоступен"; delete el.dataset.val; return; }
    setDot(dot,"good");
    if(info.latency!=null) animateNumber(el, info.latency, v=>`${v} мс`);
    else { el.textContent="работает"; delete el.dataset.val; }
  };
  put("#dotDiscord","#stDiscord",s.discord);
  put("#dotYoutube","#stYoutube",s.youtube);
  // Голос
  if(s.voice && s.voice.rtt!=null){
    const rtt=s.voice.rtt;
    setDot("#dotVoice", rtt<80?"good":rtt<160?"warn":"bad");
    animateNumber($("#stVoice"), rtt, v=>`пинг ${v} мс`);
  }else{ setDot("#dotVoice","checking"); $("#stVoice").textContent="пинг —"; delete $("#stVoice").dataset.val; }
}

// ---------- Действия ----------
function btnCenter(){
  const r=powerBtn.getBoundingClientRect();
  return {x:r.left+r.width/2, y:r.top+r.height/2};
}
// Уровень заряда молнии (0..1), управляется из JS
const boltFill=document.querySelector(".bolt-fill");
function setCharge(level, ms){
  if(!boltFill) return;
  boltFill.style.transition=`transform ${ms}ms cubic-bezier(.3,.55,.2,1)`;
  boltFill.style.transform=`translateY(${(1-level)*24}px)`;
}
// Реакция кнопки на попадание молнии / пламени
function shockwave(fire){
  const s=$("#shock"); s.className="shock"+(fire?" fire":"");
  void s.offsetWidth; s.classList.add("go");
}
function zapButton(){
  const c=btnCenter(); if(window.FX) FX.lightning(c.x,c.y);
  setTimeout(()=>{ power.classList.add("zap"); shockwave(false); setTimeout(()=>power.classList.remove("zap"),340); }, 160);
}
function scorchButton(){
  power.classList.add("scorch"); shockwave(true);
  // Пламя зажигается чуть позже — чтобы сначала было видно, как заряд стекает.
  setTimeout(()=>{ const c=btnCenter(); if(window.FX) FX.fire(c.x,c.y); }, 430);
  setTimeout(()=>power.classList.remove("scorch"),820);
}

async function togglePower(){
  if(busy) return;
  if(state.enabled){
    power.classList.remove("charged","on");   // заряд молнии плавно стекает вниз
    powerWord.textContent="OFF";
    setCharge(0, 780);                         // разрядка видна ДО пламени
    scorchButton();                            // пламя зажигается через ~0.43с
    state=await api().disable();
    renderPower(); refreshStatus();
    return;
  }
  // Включение
  if(!state.strategy){
    openSearch();  // нет стратегии — сразу автоподбор
    return;
  }
  busy=true; power.classList.add("connecting","charged"); powerWord.textContent="ON"; statusLine.textContent="Подключение…";
  zapButton();               // удар молнии по кнопке
  setCharge(0.9, 3200);      // молния медленно заряжается, пока идёт подключение
  state=await api().enable();
  // renderPower дотянет заряд до 100% ровно когда «обход активен» (или уронит до 0)
  setTimeout(async()=>{ busy=false; renderPower(); await refreshStatus(); }, 900);
}

// ---------- Поиск ----------
const overlay=$("#searchOverlay"), scanCurrent=$("#scanCurrent"), progressBar=$("#progressBar"),
      scanCount=$("#scanCount"), foundList=$("#foundList");

const scanTitle=$("#scanTitle");
let deepMode=false, deepFound=0;
function openScan(deep){
  overlay.classList.add("show");
  foundList.innerHTML=""; progressBar.style.width="0%"; scanCount.textContent="0 / 0";
  scanCurrent.textContent="инициализация…";
  scanTitle.textContent = deep ? "Кую стратегию под тебя" : "Подбираю стратегию под твоего провайдера";
  deepMode=deep; deepFound=0;
  // Радар — для автоподбора, кузница (молот+наковальня) — для глубокого поиска
  $("#radar").style.display = deep ? "none" : "";
  $("#forge").style.display = deep ? "block" : "none";
  if(deep && window.FX) FX.forgeStart($("#forge"));
  if(deep){
    if(window.pywebview && api().start_deep_search) api().start_deep_search();
    else mockDeep();
  }else{
    api().start_search();
  }
}
function openSearch(){ openScan(false); }
function openDeep(){ openScan(true); }

// Мок глубокого поиска (для браузерного превью)
function mockDeep(){
  let i=0; const total=50; const found=[];
  const t=setInterval(()=>{
    if(i>=total || found.length>=3){
      clearInterval(t);
      MockApi._state.working = found.concat(MockApi._state.working||[]);
      if(found[0]){ MockApi._state.strategy=found[0].name; MockApi._state.enabled=true; }
      window.onSearchDone(MockApi._state.working); return;
    }
    window.onSearchProgress(i,total,"Кандидат "+(i+1));
    if(i>6 && Math.random()<0.14){
      const item={id:"FreeConnect #"+(found.length+1),name:"FreeConnect #"+(found.length+1),
                  discord:3,youtube:3,latency:150+Math.round(Math.random()*50),custom:true};
      found.push(item); window.onSearchFound(item);
    }
    window.onSearchResult(i,total,"Кандидат "+(i+1),{discord:0,youtube:0});
    i++;
  }, 110);
}
window.onSearchProgress=(i,total,name)=>{
  scanCurrent.textContent="› "+name;
  scanCount.textContent=`${i} / ${total}`;
  progressBar.style.width=`${Math.round(i/total*100)}%`;
};
window.onSearchResult=(i,total,name,res)=>{
  progressBar.style.width=`${Math.round((i+1)/total*100)}%`;
  scanCount.textContent=`${i+1} / ${total}`;
};
window.onSearchFound=(item)=>{
  if(deepMode && window.FX){ deepFound++; FX.forgeSetFound(deepFound); FX.forgeBurst(); }
  const ping=$("#radarPing");
  const ang=Math.random()*Math.PI*2, rad=40+Math.random()*55;
  ping.style.left=`calc(50% + ${Math.cos(ang)*rad}px - 5px)`;
  ping.style.top=`calc(50% + ${Math.sin(ang)*rad}px - 5px)`;
  ping.classList.remove("show"); void ping.offsetWidth; ping.classList.add("show");
  const el=document.createElement("div");
  el.className="found-item"+(item.custom?" custom":"");
  const tag=item.custom?'<span class="fi-tag">своя</span>':"";
  el.innerHTML=`<span class="fi-name">✓ ${item.name}${tag}</span><span class="fi-meta">${item.latency} мс</span>`;
  foundList.appendChild(el);
};
window.onSearchDone=(working)=>{
  if(deepMode && window.FX) FX.forgeStop();
  scanCurrent.textContent = working.length ? `Найдено рабочих: ${working.length}` : "Рабочих не найдено";
  progressBar.style.width="100%";
  setTimeout(async()=>{
    overlay.classList.remove("show");
    state=await api().get_state();
    renderPower(); await refreshStatus();
    if(state.working && state.working.length) renderStrategyList(state.working);
  }, 1100);
};

// ---------- Модалка выбора ----------
const TRASH_SVG='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>';

async function deleteStrategy(name){
  state=await api().delete_strategy(name);
  if(state.working && state.working.length) renderStrategyList(state.working);
  else renderStrategyList([]);
  renderPower();
}

function renderStrategyList(working){
  const list=$("#strategyList");
  const header=`<div class="strat-head">
      <div class="strat-legend">Стратегии, которые открывают сервисы у тебя. Сверху — лучшие.</div>
      ${working.length?'<button class="strat-clear" id="clearAllBtn">Очистить все</button>':''}
    </div>`;
  if(!working.length){ list.innerHTML=header+`<div class="strat-meta">Сначала выполни автоподбор.</div>`; return; }
  list.innerHTML=header;
  const word=v=> v>=3?"работает" : v>0?"частично" : "не открыт";
  const cls =v=> v>=3?"good" : v>0?"warn" : "bad";
  working.forEach(w=>{
    const row=document.createElement("div");
    row.className="strat-row"+(w.id===state.strategy?" active":"");
    const ping = w.latency!=null ? `Пинг ${w.latency} мс` : (w.custom ? "своя стратегия" : "");
    const curTag = w.id===state.strategy ? ' <span class="cur-tag">сейчас</span>' : '';
    const ownTag = w.custom ? ' <span class="own-tag">своя</span>' : '';
    row.innerHTML=`
      <div class="strat-info">
        <div class="strat-name">${w.name}${ownTag}${curTag}</div>
        <div class="strat-meta">${ping}</div>
      </div>
      <div class="strat-right">
        <div class="strat-badges">
          <span class="svc-stat ${cls(w.youtube)}"><i></i>YouTube ${word(w.youtube)}</span>
          <span class="svc-stat ${cls(w.discord)}"><i></i>Discord ${word(w.discord)}</span>
        </div>
        <button class="strat-del" title="Удалить стратегию" aria-label="Удалить">${TRASH_SVG}</button>
      </div>`;
    row.querySelector(".strat-del").onclick=(e)=>{ e.stopPropagation(); deleteStrategy(w.name); };
    row.onclick=async(e)=>{
      if(e.target.closest(".strat-del")) return;
      $("#pickModal").classList.remove("show");
      // Показываем анимацию переключения, пока движок перезапускает winws (~4 сек).
      busy=true;
      power.classList.remove("on"); power.classList.add("connecting","charged");
      powerWord.textContent="ON";
      statusLine.textContent=`Переключаю на «${w.name}»…`; statusLine.classList.remove("on");
      currentStrategy.textContent=w.name;
      zapButton();
      setCharge(0.9, 2800);
      const t0=Date.now();
      state=await api().pick_strategy(w.id);
      const wait=Math.max(0,700-(Date.now()-t0));
      setTimeout(async()=>{ busy=false; renderPower(); await refreshStatus(); }, wait);
    };
    list.appendChild(row);
  });
  const clearBtn=$("#clearAllBtn");
  if(clearBtn){
    clearBtn.onclick=async()=>{
      if(clearBtn.dataset.armed!=="1"){
        clearBtn.dataset.armed="1";
        clearBtn.classList.add("armed");
        clearBtn.textContent="Точно? Нажми ещё раз";
        clearBtn._t=setTimeout(()=>{
          clearBtn.dataset.armed="0"; clearBtn.classList.remove("armed");
          clearBtn.textContent="Очистить все";
        }, 3000);
        return;
      }
      clearTimeout(clearBtn._t);
      state=await api().clear_strategies();
      renderStrategyList(state.working||[]);
      renderPower();
    };
  }
}

// ---------- События от бэкенда ----------
window.onVoiceUpdate=(rtt)=>{
  if(!state.enabled) return;
  if(rtt==null){ setDot("#dotVoice","bad"); $("#stVoice").textContent="потеря пакетов"; delete $("#stVoice").dataset.val; return; }
  setDot("#dotVoice", rtt<80?"good":rtt<160?"warn":"bad");
  animateNumber($("#stVoice"), rtt, v=>`пинг ${v} мс`);
};
window.onVoiceSpike=()=>{
  const d=$("#dotVoice"); d.classList.add("warn");
  $("#stVoice").textContent="скачок пинга…";
};
window.onRecovering=()=>{
  statusLine.textContent="Восстанавливаю соединение…";
  $("#stVoice").textContent="восстановление…";
  zapButton();
  setTimeout(()=>{ if(state.enabled){ statusLine.textContent="Обход активен"; statusLine.classList.add("on"); } refreshStatus(); }, 4500);
};
window.onError=(msg)=>{
  statusLine.textContent=msg;
  statusLine.classList.remove("on");
  power.classList.remove("connecting","on","charged");
  powerWord.textContent="OFF";
  busy=false;
};

// ---------- Настройки ----------
async function loadSettings(){
  if(!(window.pywebview&&window.pywebview.api&&api().get_settings)) return;
  try{
    const s=await api().get_settings();
    $("#optAutostart").checked=!!s.autostart;
    $("#optMonitor").checked=s.monitor!==false;
    if($("#optGameFilter")) $("#optGameFilter").checked=!!s.game_filter;
  }catch(e){}
}
function wireSetting(id,key){
  const el=$(id); if(!el) return;
  el.addEventListener("change",()=>{ if(api().set_setting) api().set_setting(key, el.checked); });
}
wireSetting("#optAutostart","autostart");
wireSetting("#optMonitor","monitor");
wireSetting("#optGameFilter","game_filter");

// ---------- События ----------
powerBtn.onclick=togglePower;
$("#searchBtn").onclick=openSearch;
$("#deepBtn").onclick=openDeep;
$("#cancelSearchBtn").onclick=()=>{ if(api().cancel_search) api().cancel_search(); if(deepMode&&window.FX) FX.forgeStop(); overlay.classList.remove("show"); };
$("#pickBtn").onclick=()=>{ renderStrategyList(state.working||[]); $("#pickModal").classList.add("show"); };
$("#closePick").onclick=()=>$("#pickModal").classList.remove("show");
$("#settingsBtn").onclick=()=>$("#settingsModal").classList.add("show");
$("#closeSettings").onclick=()=>$("#settingsModal").classList.remove("show");
$("#winMin").onclick=()=>{ if(api().minimize_window) api().minimize_window(); };
$("#winClose").onclick=()=>{ if(api().close_window) api().close_window(); };

// Состояние изменили извне (переключили из трея) — обновляем интерфейс
window.onExternalState=(st)=>{
  if(!st) return;
  state=st;
  renderPower(); refreshStatus();
};
document.querySelectorAll(".modal").forEach(m=>m.addEventListener("click",e=>{ if(e.target===m) m.classList.remove("show"); }));

// ---------- Онбординг-тур ----------
const TOUR=[
  {sel:"#powerBtn", text:"Главная кнопка. Нажми — молния зарядится, и обход включится: Discord и YouTube заработают. Ещё раз — выключит.", pad:18, round:"50%"},
  {sel:"#searchBtn", text:"Автоподбор: программа сама переберёт стратегии и найдёт рабочую под твоего провайдера.", pad:8},
  {sel:"#pickBtn", text:"Список рабочих стратегий — можно переключиться вручную в любой момент.", pad:8},
  {sel:"#deepBtn", text:"Глубокий поиск создаёт ТВОЮ собственную стратегию, если готовые не подошли — полная независимость.", pad:8},
  {sel:"#winControls", text:"Настройки (автозапуск, защита голосового) и сворачивание в трей — программа работает в фоне.", pad:8},
];
let tourI=0;
function showTourStep(){
  const step=TOUR[tourI];
  const el=document.querySelector(step.sel);
  if(!el){ tourI++; if(tourI>=TOUR.length){ endTour(); } else showTourStep(); return; }
  const r=el.getBoundingClientRect(), pad=step.pad||8;
  const hi=$("#tourHi");
  hi.style.left=(r.left-pad)+"px"; hi.style.top=(r.top-pad)+"px";
  hi.style.width=(r.width+pad*2)+"px"; hi.style.height=(r.height+pad*2)+"px";
  hi.style.borderRadius=step.round||"14px";
  $("#tourText").textContent=step.text;
  $("#tourNext").textContent = tourI===TOUR.length-1 ? "Готово" : "Далее";
  $("#tourProg").innerHTML=TOUR.map((_,i)=>`<span class="${i===tourI?'on':''}"></span>`).join("");
  const pop=$("#tourPop");
  const below = r.top < window.innerHeight/2;
  const left = Math.min(Math.max(12, r.left + r.width/2 - 131), window.innerWidth-274);
  pop.style.left=left+"px";
  pop.style.top = below ? (r.bottom+pad+14)+"px" : (r.top-pad-14-(pop.offsetHeight||150))+"px";
}
function isOnboarded(){
  // Основной флаг — в бэкенде (config.json), т.к. WebView2 у нас без постоянного
  // профиля и localStorage стирается между запусками. localStorage — вторичный кэш.
  return (state && state.onboarded) || localStorage.getItem("fc_onboarded")==="1";
}
function startTour(force){
  if(!force && isOnboarded()) return;
  tourI=0; $("#tour").classList.add("show");
  requestAnimationFrame(()=>requestAnimationFrame(showTourStep));
}
function nextTour(){ tourI++; if(tourI>=TOUR.length){ endTour(); return; } showTourStep(); }
function endTour(){
  $("#tour").classList.remove("show");
  localStorage.setItem("fc_onboarded","1");
  if(state) state.onboarded=true;
  try{ api().set_onboarded && api().set_onboarded(true); }catch(e){}
}
$("#tourNext").onclick=nextTour;
$("#tourSkip").onclick=endTour;
$("#replayTour").onclick=()=>{ $("#settingsModal").classList.remove("show"); startTour(true); };
$("#sendLogs").onclick=async()=>{
  const btn=$("#sendLogs"), note=$("#logsNote");
  const orig=btn.textContent; btn.disabled=true; btn.textContent="Собираю…";
  try{
    const r = api().collect_logs ? await api().collect_logs() : null;
    if(r && r.ok){
      note.innerHTML='Логи собраны в файл на рабочем столе (проводник открыт) — пришли этот .zip разработчику.';
      btn.textContent="Готово ✓";
    }else{
      note.textContent='Не удалось собрать логи'+(r&&r.error?': '+r.error:'.');
      btn.textContent=orig;
    }
  }catch(e){ note.textContent='Ошибка сбора логов'; btn.textContent=orig; }
  setTimeout(()=>{ btn.disabled=false; btn.textContent=orig; }, 3500);
};

// ---------- Обновление приложения ----------
let _updateDismissed=false;
function renderUpdate(){
  const banner=$("#updateBanner"); if(!banner) return;
  const u=(state&&state.update)||{};
  if($("#appVersion")) $("#appVersion").textContent="FreeConnect "+(state&&state.version?("v"+state.version):"");
  if(u.available && !_updateDismissed){
    $("#updateText").textContent="Доступна новая версия "+(u.version||"")+" — обновись";
    banner.hidden=false;
  }else{
    banner.hidden=true;
  }
}
if($("#updateBtn")) $("#updateBtn").onclick=()=>{
  const u=(state&&state.update)||{};
  if(u.url && api().open_url) api().open_url(u.url);
};
if($("#updateClose")) $("#updateClose").onclick=()=>{ _updateDismissed=true; $("#updateBanner").hidden=true; };
if($("#checkUpdate")) $("#checkUpdate").onclick=async()=>{
  const b=$("#checkUpdate"), o=b.textContent; b.disabled=true; b.textContent="Проверяю…";
  try{
    const u = api().check_app_update ? await api().check_app_update() : null;
    if(u){ if(state) state.update=u; renderUpdate();
      b.textContent = u.available ? ("Есть "+(u.version||"новая")) : "Актуальная версия";
    }else b.textContent=o;
  }catch(e){ b.textContent=o; }
  setTimeout(()=>{ b.disabled=false; b.textContent=o; }, 3000);
};

// ---------- Старт ----------
function waitReady(){
  // Дожидаемся моста pywebview (до ~6 сек), иначе стартуем на моке (браузер-превью).
  return new Promise(res=>{
    if(window.pywebview&&window.pywebview.api) return res();
    let n=0; const t=setInterval(()=>{ if((window.pywebview&&window.pywebview.api)||++n>60){clearInterval(t);res();} },100);
  });
}

// --- Сплэш / стартовая автоматизация ---
const splashFill=document.querySelector("#splashFill");
function setSplashCharge(level){
  if(splashFill) splashFill.style.transform=`translateY(${(1-level)*24}px)`;
}
window.onStartupStep=(pct,label,status,detail)=>{
  $("#splashPct").textContent=`${pct}%`;
  $("#splashStep").textContent=detail ? `${label} — ${detail}` : label;
  setSplashCharge(pct/100);
};
window.onStartupDone=(results)=>{
  setSplashCharge(1); $("#splashPct").textContent="100%"; $("#splashStep").textContent="готово";
  fmark("onStartupDone -> hide splash in 700ms");
  setTimeout(()=>{ fmark("splash hidden, calling afterStartup"); $("#splash").classList.add("hide"); afterStartup(); }, 700);
};

const MOCK_STEPS=["Проверка сетевого драйвера","Оптимизация TCP","Поиск конфликтов",
  "Обновление списков Discord/YouTube","Проверка обновлений стратегий","Загрузка стратегий"];
function mockStartup(){
  let i=0;
  const t=setInterval(()=>{
    if(i>=MOCK_STEPS.length){ clearInterval(t); window.onStartupDone([]); return; }
    window.onStartupStep(Math.round((i+1)/MOCK_STEPS.length*100), MOCK_STEPS[i], "ok", "");
    i++;
  }, 520);
}

// Насос событий бэкенд->UI. Фон кладёт события в очередь Python, здесь мы
// забираем их В UI-ПОТОКЕ и вызываем window.onX. Так ни один evaluate_js не
// идёт из фонового потока (иначе WebView2 висит с COM-исключениями).
let eventPumpOn=false;
function startEventPump(){
  if(eventPumpOn || !(window.pywebview && window.pywebview.api && api().poll_events)) return;
  eventPumpOn=true;
  let busyPump=false;
  setInterval(async()=>{
    if(busyPump) return;
    busyPump=true;
    try{
      const evs=await api().poll_events();
      if(evs && evs.length){
        for(const e of evs){
          const fn=window[e.fn];
          if(typeof fn==="function"){ try{ fn.apply(null, e.args||[]); }catch(_){} }
        }
      }
    }catch(_){}
    busyPump=false;
  }, 250);
}

async function afterStartup(){
  fmark("afterStartup begin");
  try{ fmark("call is_frameless"); if(api().is_frameless && await api().is_frameless()) document.body.classList.add("frameless"); fmark("is_frameless done"); }catch(e){}
  try{ fmark("call get_state"); state=await api().get_state(); fmark("get_state done"); }catch(e){}
  fmark("call loadSettings"); await loadSettings(); fmark("loadSettings done");
  renderPower(); fmark("renderPower done");
  renderUpdate();
  refreshStatus(); fmark("refreshStatus kicked");
  startEventPump();               // забираем события бэкенда (поиск/голос/восстановление)
  setInterval(()=>{ if(state.enabled && !busy) refreshStatus(); }, 5000);
  // Проверка версии приложения идёт в фоне и появляется в state.update позже —
  // перечитываем состояние несколько раз в первые ~минуту, чтобы поймать баннер.
  let upTries=0;
  const upTimer=setInterval(async()=>{
    if(++upTries>6){ clearInterval(upTimer); return; }
    try{ const st=await api().get_state(); if(st){ state.update=st.update; state.version=st.version; renderUpdate();
      if(st.update && st.update.available) clearInterval(upTimer); } }catch(e){}
  }, 10000);
  // Авто-обучение только при первом запуске (флаг onboarded из config.json —
  // переживает перезапуск/перезагрузку, в отличие от localStorage WebView2).
  if(!isOnboarded()) setTimeout(()=>startTour(false), 700);
}

// Реальный старт: диагностика — это ФОНОВОЕ обслуживание (обновление списков,
// проверка версий, включение служб). Она НЕ должна задерживать вход в приложение:
// раньше сплэш висел, пока не отработают сетевые шаги (таймауты по 8с) — из-за
// этого «зависание после открытия на несколько секунд». Теперь показываем короткую
// анимацию заряда и сразу пускаем в интерфейс; диагностика доигрывает в фоне.
const SPLASH_MAX_MS=1500;   // не держим пользователя у сплэша дольше этого
async function realStartup(){
  fmark("realStartup begin -> start_diagnostics");
  try{ await api().start_diagnostics(); }catch(e){}
  fmark("start_diagnostics returned");
  const t0=Date.now();
  for(;;){
    let p;
    try{ p=await api().get_startup_progress(); }catch(e){ break; }
    if(p && p.label) window.onStartupStep(p.pct, p.label, p.status, p.detail);
    if(!p || p.done) break;                       // диагностика закончилась раньше — отлично
    if(Date.now()-t0 > SPLASH_MAX_MS) break;      // ещё идёт — но НЕ ждём её, пускаем в приложение
    await new Promise(r=>setTimeout(r, 120));
  }
  window.onStartupDone([]);   // прячет сплэш и открывает интерфейс; фон продолжит диагностику сам
}

(async function boot(){
  fmark("boot begin -> waitReady");
  await waitReady();
  fmark("waitReady done (bridge ready="+!!(window.pywebview&&window.pywebview.api)+")");
  fflush();   // как только мост готов — сразу сливаем всё накопленное в debug.log
  if(window.pywebview && window.pywebview.api && api().start_diagnostics){
    realStartup();                // реальная стартовая автоматизация (фон + опрос)
  }else{
    mockStartup();                // браузерный превью-режим
  }
})();
