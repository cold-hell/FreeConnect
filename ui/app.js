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
  async manual_voice_switch(){
    const w=(this._state.working||[]).filter(x=>x.name!==this._state.strategy);
    if(!w.length) return {switched:false, name:"", reason:"no_candidates"};
    this._state.strategy=w[0].name; this._state.enabled=true;
    return {switched:true, name:w[0].name, reason:""};
  },
  async status(){
    if(!this._state.enabled) return {discord:null,youtube:null,voice:null};
    return {discord:{ok:true,latency:142},youtube:{ok:true,latency:168},voice:{rtt:38}};
  },
  // VPN-для-Discord (мок для отладки дизайна в браузере — реальная логика в бэкенде).
  _vpn:{servers:[], selected:"auto", enabled:false},
  async vpn_get_state(){
    return {available:false, imported:this._vpn.servers.length>0, servers:this._vpn.servers,
            selected:this._vpn.selected, enabled:this._vpn.enabled, sub_url:""};
  },
  async vpn_import(url, json){
    if(!url && !json) return {...await this.vpn_get_state(), ok:false, error:"Вставь ссылку или JSON"};
    this._vpn.servers=[
      {id:"finland",country:"finland",name:"Финляндия",sub:"Hysteria2"},
      {id:"germany",country:"germany",name:"Германия",sub:"Hysteria2"},
      {id:"italy",country:"italy",name:"Италия",sub:"Hysteria2"},
      {id:"netherlands",country:"netherlands",name:"Нидерланды",sub:"Hysteria2"},
      {id:"poland",country:"poland",name:"Польша",sub:"VLESS-Reality"},
      {id:"japan",country:"japan",name:"Япония",sub:"VLESS-Reality"},
      {id:"france",country:"france",name:"Франция",sub:"Hysteria2"},
      {id:"united-kingdom",country:"united-kingdom",name:"Великобритания",sub:"VLESS-Reality"},
    ];
    return {...await this.vpn_get_state(), ok:true, message:"Импортировано стран: "+this._vpn.servers.length+" (мок)"};
  },
  async vpn_select(country){ this._vpn.selected=country==="auto"?"auto":country; return {...await this.vpn_get_state(), ok:true}; },
  // Обход Telegram (мок для отладки дизайна в браузере).
  _tg:{enabled:false},
  async tg_get_state(){ return {available:true, enabled:this._tg.enabled, port:1080, host:"127.0.0.1", deeplink:"tg://socks?server=127.0.0.1&port=1080", autostart:false}; },
  async tg_set_enabled(on){ this._tg.enabled=!!on; if(window.onTgState) window.onTgState(!!on);
    return {...await this.tg_get_state(), ok:true, message: on?"Прокси Telegram включён (мок)":"Обход Telegram выключен"}; },
  async tg_autoconfigure(){ this._tg.enabled=true; if(window.onTgState) window.onTgState(true);
    return {...await this.tg_get_state(), ok:true, message:"Открыл Telegram (мок)"}; },
  async tg_discover(){ setTimeout(()=>{ if(window.onTgDiscover) window.onTgDiscover({stage:"scan",done:128,total:256});
    setTimeout(()=>{ if(window.onTgDiscoverDone) window.onTgDiscoverDone({ok:true,found:["149.154.167.220"],
      message:"Найден рабочий адрес: 149.154.167.220 (мок)"}); }, 600); }, 300);
    return {ok:true, started:true}; },
  async tg_diagnose(){ return {ok:true, verdict:"Обход Telegram работает — живой адрес: 149.154.167.220 (мок)",
    hint:"", host:"kws2.web.telegram.org", dns:["149.154.167.99"], rows:[
      {ip:"149.154.167.220", source:"встроенный", tcp:"ok 45мс", tls:"ok", ws:"ok (101)", ok:true},
      {ip:"149.154.167.99", source:"DNS", tcp:"нет ответа (блокировка)", tls:"—", ws:"—", ok:false}]}; },
  async vpn_set_enabled(on){
    this._vpn.enabled=!!on;
    if(window.onVpnState) window.onVpnState(!!on);
    return {...await this.vpn_get_state(), ok:true, message: on?"Discord идёт через VPN (мок)":"VPN для Discord выключен"};
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
  // Кнопка ручной смены стратегии видна, только когда обход включён (иначе нечего чинить).
  const vsb=$("#voiceSwitchBtn"); if(vsb) vsb.style.display = state.enabled ? "" : "none";
  // Заряд молнии дотягивается до 100% ровно когда обход активен (или падает до 0)
  setCharge(state.enabled ? 1 : 0, 450);
  // Бейдж «Discord через VPN» синхронизируем с реальным состоянием туннеля.
  if(typeof vpnSetBadge==="function") vpnSetBadge(!!state.vpn_on);
  if(typeof tgSetBadge==="function") tgSetBadge(!!state.tg_on);
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
// Реакция кнопки: всплеск энергии (вкл) / разрядка (выкл)
function shockwave(down){
  const s=$("#shock"); s.className="shock"+(down?" down":"");
  void s.offsetWidth; s.classList.add("go");
}
function zapButton(){
  const c=btnCenter(); if(window.FX) FX.surge(c.x,c.y);
  setTimeout(()=>{ power.classList.add("zap"); shockwave(false); setTimeout(()=>power.classList.remove("zap"),480); }, 120);
}
function dischargeButton(){
  power.classList.add("powerdown"); shockwave(true);
  // Разрядка стартует чуть позже — сначала видно, как заряд стекает вниз.
  setTimeout(()=>{ const c=btnCenter(); if(window.FX) FX.discharge(c.x,c.y); }, 380);
  setTimeout(()=>power.classList.remove("powerdown"),820);
}

async function togglePower(){
  if(busy) return;
  if(state.enabled){
    power.classList.remove("charged","on");   // заряд молнии плавно стекает вниз
    powerWord.textContent="OFF";
    setCharge(0, 780);                         // заряд стекает вниз ДО разрядки
    dischargeButton();                         // свет схлопывается через ~0.38с
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
// Гасит Space/стрелку на короткое время (после закрытия оверлея), чтобы залётный
// пробел из дино-игры не «нажал» сфокусированную кнопку и не перезапустил поиск.
function _guardKeysBriefly(ms){
  ms = ms || 800; const until = Date.now() + ms;
  const h = (e)=>{ if(e.code==="Space"||e.code==="ArrowUp"){ e.preventDefault(); e.stopPropagation(); } };
  document.addEventListener("keydown", h, true);
  setTimeout(()=>document.removeEventListener("keydown", h, true), ms);
  if(document.activeElement && document.activeElement.blur) document.activeElement.blur();
}
function openScan(deep){
  if(document.activeElement && document.activeElement.blur) document.activeElement.blur(); // кнопка не должна ловить пробел
  overlay.classList.add("show");
  // сброс возможных остатков гайд-проверки голоса
  $("#vcPanel").style.display="none"; $("#foundList").style.display=""; $("#dinoWrap").style.display="";
  if(window.Dino) requestAnimationFrame(()=>Dino.start($("#dinoCanvas")));
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
  if(refreshMode) scanCurrent.textContent = `Список обновлён: ${working.length}`;
  else scanCurrent.textContent = working.length ? `Найдено рабочих: ${working.length}` : "Рабочих не найдено";
  progressBar.style.width="100%";
  setTimeout(async()=>{
    overlay.classList.remove("show");
    if(window.Dino) Dino.stop();
    _guardKeysBriefly(800);           // залётный пробел из дино не должен перезапустить поиск
    state=await api().get_state();
    renderPower(); await refreshStatus();
    if(state.working && state.working.length) renderStrategyList(state.working);
    if(refreshMode){ refreshMode=false; $("#pickModal").classList.add("show"); }  // вернуть список
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
      <div class="strat-head-btns">
        ${working.length?'<button class="strat-refresh" id="refreshListBtn" aria-label="Обновить список" title="Пере-тест: заново проверить пинг и доступность Discord/YouTube, отсортировать"><svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M12 4V1L8 5l4 4V6c3.31 0 6 2.69 6 6 0 1.01-.25 1.97-.7 2.8l1.46 1.46C19.54 15.03 20 13.57 20 12c0-4.42-3.58-8-8-8zm0 14c-3.31 0-6-2.69-6-6 0-1.01.25-1.97.7-2.8L5.24 7.74C4.46 8.97 4 10.43 4 12c0 4.42 3.58 8 8 8v3l4-4-4-4v3z"/></svg></button>':''}
        ${working.length?'<button class="strat-clear" id="clearAllBtn">Очистить все</button>':''}
      </div>
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
  const refreshBtn=$("#refreshListBtn");
  if(refreshBtn) refreshBtn.onclick=openRefresh;
}

// Пере-тест сохранённых стратегий (кнопка «↻ Обновить» в списке): переиспользуем
// оверлей поиска как индикатор прогресса. Ничего не генерируем — только заново меряем.
let refreshMode=false;
function openRefresh(){
  if(document.activeElement && document.activeElement.blur) document.activeElement.blur();
  refreshMode=true;
  $("#pickModal").classList.remove("show");   // спрячем список, вернём после
  overlay.classList.add("show");
  $("#vcPanel").style.display="none"; $("#foundList").style.display=""; $("#dinoWrap").style.display="";
  $("#radar").style.display=""; $("#forge").style.display="none";
  foundList.innerHTML=""; progressBar.style.width="0%"; scanCount.textContent="0 / 0";
  deepMode=false; deepFound=0;
  scanTitle.textContent="Обновляю список стратегий";
  scanCurrent.textContent="перезапускаю по очереди и меряю…";
  if(window.Dino) requestAnimationFrame(()=>Dino.start($("#dinoCanvas")));
  if(window.pywebview && api().refresh_strategies) api().refresh_strategies();
  else mockRefresh();
}
// Мок пере-теста для браузерного превью.
function mockRefresh(){
  const items=(MockApi._state.working||[]).slice();
  let i=0; const total=items.length||1;
  const t=setInterval(()=>{
    if(i>=items.length){
      clearInterval(t);
      items.sort((a,b)=>((b.discord>=3)+(b.youtube>=3))-((a.discord>=3)+(a.youtube>=3)) || (a.latency||9999)-(b.latency||9999));
      MockApi._state.working=items;
      window.onSearchDone(items); return;
    }
    const w=items[i];
    if(w) w.latency=120+Math.round(Math.random()*120);
    window.onSearchProgress(i,total,(w&&w.name)||"?");
    window.onSearchResult(i,total,(w&&w.name)||"?",{discord:0,youtube:0});
    i++;
  }, 260);
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
window.onServiceDegraded=(info)=>{
  // Watchdog заметил, что сервис перестал открываться — подсветим и покажем восстановление.
  const svc=(info&&info.service)||"";
  const map={discord:["#dotDiscord","#stDiscord"], youtube:["#dotYoutube","#stYoutube"]};
  const sel=map[svc];
  if(sel){ const d=$(sel[0]); if(d) d.classList.add("warn"); const s=$(sel[1]); if(s) s.textContent="проверяю…"; }
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
    if($("#optDoh")) $("#optDoh").checked=!!s.doh;
    if($("#optVoiceConfirm")) $("#optVoiceConfirm").checked=!!s.voice_confirm;
  }catch(e){}
}
function wireSetting(id,key){
  const el=$(id); if(!el) return;
  el.addEventListener("change",()=>{ if(api().set_setting) api().set_setting(key, el.checked); });
}
wireSetting("#optAutostart","autostart");
wireSetting("#optMonitor","monitor");
wireSetting("#optGameFilter","game_filter");
wireSetting("#optDoh","doh");
wireSetting("#optVoiceConfirm","voice_confirm");
// «Авто-восстановление связи» (optMonitor) теперь охватывает и живость голоса —
// отдельного тумблера voice_watch в UI больше нет (детектор идёт под монитором).
// Детектор голоса сообщил, что голос односторонний/мёртвый — уведомляем и подсказываем.
window.onVoiceDead=(info)=>{
  toast("Голос замолчал — чиню. Если не поможет, смени регион голосового канала (Настройки сервера → Регион).", "warn");
};
// Перебраны все стратегии, голос так и не поднялся — это уже путь/RTC-сервер, а не стратегия.
window.onRecoveryExhausted=()=>{
  toast("Перепробовал все стратегии — голос не держится. Похоже, дело в Discord-сервере: смени РЕГИОН голосового канала (Настройки канала → Регион).", "warn");
};
// Отражаем реальный результат включения DoH (смена DNS идёт в фоне и может не удаться).
window.onDohState=(ok)=>{
  const el=$("#optDoh"); if(el) el.checked=!!ok;
  const note=$("#dohNote"); if(note) note.style.display = ok ? "none" : "block";
  if(!ok && api().set_setting) api().set_setting("doh", false);
};

// ---------- События ----------
powerBtn.onclick=togglePower;
$("#searchBtn").onclick=openSearch;
// Глубокий поиск: если включена точная проверка голоса — сначала гайд «зайди в канал».
$("#deepBtn").onclick=async()=>{
  let vc=false;
  try{ if(window.pywebview&&api().get_settings){ const s=await api().get_settings(); vc=!!s.voice_confirm; } }catch(e){}
  if(vc) $("#voiceSetupModal").classList.add("show");
  else openDeep();
};
$("#voiceSetupCancel").onclick=()=>$("#voiceSetupModal").classList.remove("show");
$("#voiceSetupDone").onclick=()=>{ $("#voiceSetupModal").classList.remove("show"); openDeep(); };
// Гайд: обход включён на бутстрап-стратегии — Discord уже может открыться.
window.onGuidedBootstrap=(info)=>{
  scanTitle.textContent="Обход включён — подбираю стратегию";
  scanCurrent.textContent="Открой Discord (в канал пока не заходи) — сейчас попросим проверить голос.";
};
// Гайд-подтверждение голоса: бэкенд включил кандидата и ждёт вердикт человека.
window.onVoiceConfirmProbe=(info)=>{
  if(window.FX) FX.forgeStop(); if(window.Dino) Dino.stop();
  $("#forge").style.display="none"; $("#dinoWrap").style.display="none"; $("#foundList").style.display="none";
  scanTitle.textContent=`Проверка голоса — кандидат ${info.index} / ${info.total}`;
  scanCurrent.textContent="Обход включён — зайди в голосовой канал Discord.";
  $("#vcPanel").style.display="block";
};
$("#vcYes").onclick=()=>{ if(api().voice_confirm_result) api().voice_confirm_result(true); $("#vcPanel").style.display="none"; scanCurrent.textContent="Фиксирую стратегию…"; };
$("#vcNext").onclick=()=>{ if(api().voice_confirm_result) api().voice_confirm_result(false); $("#vcPanel").style.display="none"; scanCurrent.textContent="Пробую следующую стратегию…"; };
window.onVoiceConfirmDone=(res)=>{
  $("#vcPanel").style.display="none";
  if(res && res.confirmed){ scanTitle.textContent="Готово!"; scanCurrent.textContent="Голос подтверждён: "+res.name; }
  else if(res && res.empty){ scanTitle.textContent="Не получилось"; scanCurrent.textContent="Discord нигде не открылся — попробуй ещё раз"; }
  else if(res && res.cancelled){ scanCurrent.textContent="Отменено"; }
  else { scanTitle.textContent="Голос не подтвердился"; scanCurrent.textContent="Ни на одной стратегии голос не подключился"; }
  setTimeout(async()=>{
    overlay.classList.remove("show"); _guardKeysBriefly(800);
    $("#forge").style.display=""; $("#dinoWrap").style.display=""; $("#foundList").style.display="";
    if(window.pywebview){ state=await api().get_state(); renderPower(); await refreshStatus(); if(state.working&&state.working.length) renderStrategyList(state.working); }
  }, 1700);
};
// Лёгкий тост (эфемерное уведомление) — для результата ручной смены стратегии.
let _toastT=null;
function toast(msg, kind){
  let t=$("#toast");
  if(!t){ t=document.createElement("div"); t.id="toast"; document.body.appendChild(t); }
  t.textContent=msg; t.className="toast show"+(kind?(" "+kind):"");
  clearTimeout(_toastT); _toastT=setTimeout(()=>t.classList.remove("show"), 3200);
}
// Ручная смена стратегии при лагающем голосе: авто-детект смерти Discord-войса без
// залогина невозможен, поэтому переключаем по клику на следующую Discord-стратегию.
let _switchBusy=false;
$("#voiceSwitchBtn").onclick=async()=>{
  if(_switchBusy) return;
  if(!api().manual_voice_switch){ toast("Недоступно в этом режиме"); return; }
  _switchBusy=true;
  const btn=$("#voiceSwitchBtn"); const old=btn.textContent;
  btn.disabled=true; btn.textContent="Переключаю…";
  try{
    const r=await api().manual_voice_switch();
    if(r && r.switched){
      toast("Переключил на: "+r.name+". Проверь голос в Discord.", "ok");
      if(window.pywebview){ state=await api().get_state(); renderPower(); await refreshStatus(); }
    }else if(r && r.reason==="no_candidates"){
      toast("Нет других Discord-стратегий — запусти подбор заново.", "warn");
    }else{
      toast("Не удалось переключиться. Попробуй подбор заново.", "warn");
    }
  }catch(e){ toast("Ошибка переключения", "warn"); }
  finally{ _switchBusy=false; btn.disabled=false; btn.textContent=old; }
};
$("#cancelSearchBtn").onclick=()=>{ if(api().cancel_search) api().cancel_search(); if(deepMode&&window.FX) FX.forgeStop(); if(window.Dino) Dino.stop(); overlay.classList.remove("show"); _guardKeysBriefly(800); };
$("#pickBtn").onclick=()=>{ renderStrategyList(state.working||[]); $("#pickModal").classList.add("show"); };
$("#closePick").onclick=()=>$("#pickModal").classList.remove("show");
$("#settingsBtn").onclick=()=>$("#settingsModal").classList.add("show");
$("#closeSettings").onclick=()=>$("#settingsModal").classList.remove("show");

// ---------- VPN для Discord (пока КАРКАС: обработчики — заглушки/мок) ----------
$("#openVpn").onclick=async()=>{ $("#settingsModal").classList.remove("show"); $("#vpnModal").classList.add("show"); await vpnRefresh(); };
$("#closeVpn").onclick=()=>$("#vpnModal").classList.remove("show");
$("#vpnPasteJsonBtn").onclick=()=>{
  const t=$("#vpnJson"); t.style.display = t.style.display==="none" ? "block" : "none";
  if(t.style.display==="block") t.focus();
};
function vpnSetStatus(text, on){
  $("#vpnStatusText").textContent=text;
  $("#vpnStatus").classList.toggle("on", !!on);
}

// Круглые флаги стран рисуем SVG-ом: Windows не рендерит эмодзи-флаги
// (показывает буквенные пары «FI», «DE»). Квадрат 24×24 обрезается в круг
// контейнером .vpn-flag (overflow:hidden). Ключи стран = ключи vpn.COUNTRIES.
const VPN_FLAGS={
  finland:`<rect width="24" height="24" fill="#fff"/><rect x="7" width="4" height="24" fill="#003580"/><rect y="10" width="24" height="4" fill="#003580"/>`,
  germany:`<rect width="24" height="8" fill="#000"/><rect y="8" width="24" height="8" fill="#d00"/><rect y="16" width="24" height="8" fill="#ffce00"/>`,
  netherlands:`<rect width="24" height="8" fill="#ae1c28"/><rect y="8" width="24" height="8" fill="#fff"/><rect y="16" width="24" height="8" fill="#21468b"/>`,
  poland:`<rect width="24" height="12" fill="#fff"/><rect y="12" width="24" height="12" fill="#dc143c"/>`,
  japan:`<rect width="24" height="24" fill="#fff"/><circle cx="12" cy="12" r="6" fill="#bc002d"/>`,
  italy:`<rect width="8" height="24" fill="#009246"/><rect x="8" width="8" height="24" fill="#fff"/><rect x="16" width="8" height="24" fill="#ce2b37"/>`,
  france:`<rect width="8" height="24" fill="#0055a4"/><rect x="8" width="8" height="24" fill="#fff"/><rect x="16" width="8" height="24" fill="#ef4135"/>`,
  "united-kingdom":`<rect width="24" height="24" fill="#012169"/><path d="M0,0 L24,24 M24,0 L0,24" stroke="#fff" stroke-width="4"/><path d="M0,0 L24,24 M24,0 L0,24" stroke="#c8102e" stroke-width="2"/><path d="M12,0 V24 M0,12 H24" stroke="#fff" stroke-width="6"/><path d="M12,0 V24 M0,12 H24" stroke="#c8102e" stroke-width="3.5"/>`,
  // «Авто» и незнакомые страны — приглушённый глобус (как «Авто» в Happ).
  auto:`<rect width="24" height="24" fill="#1b2233"/><g fill="none" stroke="#8b93ad" stroke-width="1.3"><circle cx="12" cy="12" r="8"/><ellipse cx="12" cy="12" rx="3.6" ry="8"/><path d="M4 12h16M5.6 7.2h12.8M5.6 16.8h12.8"/></g>`,
};
function vpnFlag(country){
  return `<span class="vpn-flag"><svg viewBox="0 0 24 24">${VPN_FLAGS[country]||VPN_FLAGS.auto}</svg></span>`;
}
const VPN_CHECK=`<svg class="vpn-srv-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>`;

let vpnServers=[];       // список стран-выходов из бэкенда (без строки «Авто»)
let vpnSelectedId="auto";

// Рисует кастомный список серверов в стиле выбора стратегий (строки-карточки
// с круглым флагом, кликом выбираем; выбранная подсвечена и с галочкой).
function vpnRenderServers(note){
  const box=$("#vpnServer");
  if(!vpnServers.length){
    box.innerHTML="";
    if(note!==undefined) $("#vpnServerNote").textContent=note||"Импортируй подписку, чтобы выбрать страну.";
    return;
  }
  const rows=[{id:"auto",country:"auto",name:"Авто",sub:"лучший по пингу · Hysteria2 в приоритете"}]
    .concat(vpnServers);
  if(!rows.some(r=>r.id===vpnSelectedId)) vpnSelectedId="auto";
  box.innerHTML="";
  rows.forEach(r=>{
    const row=document.createElement("div");
    row.className="vpn-srv-row"+(r.id===vpnSelectedId?" active":"");
    row.innerHTML=`${vpnFlag(r.country)}
      <div class="vpn-srv-body">
        <div class="vpn-srv-name">${r.name}</div>
        <div class="vpn-srv-sub">${r.sub||""}</div>
      </div>${VPN_CHECK}`;
    row.onclick=async()=>{
      if(vpnSelectedId===r.id) return;
      vpnSelectedId=r.id;
      vpnRenderServers();            // мгновенная подсветка
      try{
        const st=await api().vpn_select(r.id);
        if(st){ if(st.ok===false && st.error) toast(st.error,"warn");
                else if(st.message) toast(st.message,"ok");
                vpnApplyState(st); }
      }catch(e){}
    };
    box.appendChild(row);
  });
  if(note!==undefined) $("#vpnServerNote").textContent=note;
}

// Применяет состояние VPN из бэкенда к окну (список, выбор, статус, тумблер).
function vpnApplyState(st){
  if(!st) return;
  vpnServers=st.servers||[];
  vpnSelectedId=st.selected||"auto";
  if(st.sub_url && $("#vpnSubUrl") && !$("#vpnSubUrl").value.trim()) $("#vpnSubUrl").value=st.sub_url;
  $("#optVpnOn").checked=!!st.enabled;
  if(st.enabled) vpnSetStatus("Discord идёт через VPN — включён", true);
  else if(st.imported) vpnSetStatus("Импортировано — выбери сервер и включи", false);
  else vpnSetStatus(st.available===false?"Не настроен (обнови приложение для VPN)":"Не настроен", false);
  const note = st.imported ? ("Найдено стран: "+vpnServers.length+".")
                           : "Импортируй подписку, чтобы выбрать страну.";
  vpnRenderServers(note);
  vpnSetBadge(!!st.enabled);
}
async function vpnRefresh(){
  try{ vpnApplyState(await api().vpn_get_state()); }catch(e){}
}

$("#vpnImport").onclick=async()=>{
  const url=$("#vpnSubUrl").value.trim(), json=$("#vpnJson").value.trim();
  if(!url && !json){ toast("Вставь ссылку-подписку или конфиг JSON","warn"); return; }
  const btn=$("#vpnImport"), old=btn.textContent;
  btn.disabled=true; btn.textContent="Импорт…";
  try{
    const st=await api().vpn_import(url, json);
    if(st && st.ok===false){ toast(st.error||"Не удалось импортировать","warn"); }
    else if(st){ toast(st.message||"Импортировано","ok"); }
    vpnApplyState(st);
  }catch(e){ toast("Ошибка импорта","warn"); }
  finally{ btn.disabled=false; btn.textContent=old; }
};

$("#optVpnOn").onchange=async(e)=>{
  const want=e.target.checked;
  e.target.disabled=true;
  try{
    const st=await api().vpn_set_enabled(want);
    if(st && st.ok===false){ toast(st.error||"Не удалось включить VPN","warn"); e.target.checked=!want; }
    else if(st && st.message){ toast(st.message, want?"ok":""); }
    if(st) vpnApplyState(st);
  }catch(err){ e.target.checked=!want; toast("Ошибка VPN","warn"); }
  finally{ e.target.disabled=false; }
};

// Бейдж «Discord через VPN» на главном экране (C2): ненавязчивый индикатор.
function vpnSetBadge(on){
  const b=$("#vpnBadge");
  if(b) b.classList.toggle("show", !!on);
}
window.onVpnState=(on)=>vpnSetBadge(!!on);

// ---------- Обход Telegram (локальный SOCKS5→WebSocket, см. tgproxy.py) ----------
$("#openTg").onclick=async()=>{ $("#settingsModal").classList.remove("show"); $("#tgModal").classList.add("show"); await tgRefresh(); };
$("#closeTg").onclick=()=>$("#tgModal").classList.remove("show");

function tgSetStatus(text, on){
  $("#tgStatusText").textContent=text;
  $("#tgStatus").classList.toggle("on", !!on);
}
function tgApplyState(st){
  if(!st) return;
  $("#optTgOn").checked=!!st.enabled;
  if(st.host && st.port) $("#tgManualAddr").textContent=st.host+":"+st.port;
  if(st.enabled) tgSetStatus("Включён — Telegram идёт через прокси", true);
  else tgSetStatus(st.available===false?"Недоступно (обнови приложение)":"Выключен", false);
  // Без автозапуска Telegram умрёт после перезагрузки — предупреждаем заранее.
  const warn=$("#tgAutostartWarn");
  if(warn) warn.hidden = !(st.enabled && st.autostart===false);
  tgSetBadge(!!st.enabled);
}
$("#tgAutostartBtn").onclick=async()=>{
  const btn=$("#tgAutostartBtn"), old=btn.textContent;
  btn.disabled=true; btn.textContent="Включаю…";
  try{
    await api().set_setting("autostart", true);
    const cb=$("#optAutostart"); if(cb) cb.checked=true;
    toast("Автозапуск включён — Telegram будет работать после перезагрузки","ok");
    await tgRefresh();
  }catch(e){ toast("Не удалось включить автозапуск","warn"); }
  finally{ btn.disabled=false; btn.textContent=old; }
};
async function tgRefresh(){
  try{ tgApplyState(await api().tg_get_state()); }catch(e){}
}
$("#tgAutoBtn").onclick=async()=>{
  const btn=$("#tgAutoBtn"), old=btn.textContent;
  btn.disabled=true; btn.textContent="Открываю Telegram…";
  try{
    const st=await api().tg_autoconfigure();
    if(st && st.ok===false){ toast(st.error||"Не удалось настроить","warn"); }
    else if(st){ toast(st.message||"Готово","ok"); }
    tgApplyState(st);
  }catch(e){ toast("Ошибка автонастройки","warn"); }
  finally{ btn.disabled=false; btn.textContent=old; }
};
// Диагностика: показывает, на какой ступени рвётся (блокировка IP / TLS / веб-сокет).
$("#tgCheckBtn").onclick=async()=>{
  const btn=$("#tgCheckBtn"), old=btn.textContent;
  btn.disabled=true; btn.textContent="Проверяю…";
  try{
    const r=await api().tg_diagnose();
    const box=$("#tgDiag"); box.hidden=false;
    const v=$("#tgDiagVerdict");
    v.textContent=r.verdict||""; v.classList.toggle("bad", !r.ok);
    // Подсказка есть не всегда; пустой .settings-note рисовался бы пустой жёлтой рамкой.
    const hint=$("#tgDiagHint");
    hint.textContent=r.hint||""; hint.hidden=!r.hint;
    // Все адреса молчат = их заблокировали: предлагаем поискать новый.
    $("#tgFindBtn").hidden = !!r.ok;
    $("#tgDiagRows").innerHTML=(r.rows||[]).map(row=>
      `<div class="tg-diag-row ${row.ok?"ok":"bad"}">`+
      `<b>${row.ip}</b> <span class="tg-diag-src">${row.source||""}</span>`+
      `<span>связь: ${row.tcp}</span><span>шифрование: ${row.tls}</span>`+
      `<span>канал: ${row.ws}</span></div>`).join("");
  }catch(e){ toast("Не удалось выполнить проверку","warn"); }
  finally{ btn.disabled=false; btn.textContent=old; }
};
// Автопоиск живого адреса — на случай, если встроенный однажды заблокируют.
$("#tgFindBtn").onclick=async()=>{
  const btn=$("#tgFindBtn");
  btn.disabled=true; btn.textContent="Ищу…";
  const p=$("#tgFindProgress"); p.hidden=false;
  p.textContent="Готовлюсь к поиску…";
  try{
    const r=await api().tg_discover();
    if(r && r.ok===false){ toast(r.error||"Не удалось запустить поиск","warn"); tgFindReset(); }
  }catch(e){ toast("Не удалось запустить поиск","warn"); tgFindReset(); }
};
function tgFindReset(){
  const btn=$("#tgFindBtn");
  btn.disabled=false; btn.textContent="Найти рабочий адрес";
}
window.onTgDiscover=(p)=>{
  const el=$("#tgFindProgress");
  if(!el||!p) return;
  el.hidden=false;
  el.textContent = p.stage==="verify"
    ? `Проверяю найденные узлы: ${p.done} из ${p.total}…`
    : `Перебираю адреса: ${p.done} из ${p.total}…`;
};
window.onTgDiscoverDone=(r)=>{
  tgFindReset();
  const el=$("#tgFindProgress");
  if(el){ el.hidden=false; el.textContent=(r&&r.message)||""; }
  if(r&&r.ok){ toast(r.message||"Найден рабочий адрес","ok"); $("#tgFindBtn").hidden=true; }
  else if(r){ toast(r.message||"Живых адресов не нашлось","warn"); }
};
$("#optTgOn").onchange=async(e)=>{
  const want=e.target.checked;
  // Выключение = Telegram перестаёт работать: он настроен ходить через наш прокси.
  if(!want && !confirm("Выключить соединение Telegram?\n\nTelegram настроен ходить через наш прокси, "+
                       "поэтому пока обход выключен, он работать не будет.\n\n"+
                       "Чтобы вернуть Telegram без обхода, отключи прокси в самом Telegram: "+
                       "Настройки → Продвинутые → Тип соединения.")){
    e.target.checked=true; return;
  }
  e.target.disabled=true;
  try{
    const st=await api().tg_set_enabled(want);
    if(st && st.ok===false){ toast(st.error||"Не удалось включить соединение Telegram","warn"); e.target.checked=!want; }
    else if(st && st.message){ toast(st.message, want?"ok":""); }
    if(st) tgApplyState(st);
  }catch(err){ e.target.checked=!want; toast("Ошибка соединения Telegram","warn"); }
  finally{ e.target.disabled=false; }
};
// Бейдж «Telegram через прокси» на главном экране.
function tgSetBadge(on){
  const b=$("#tgBadge");
  if(b) b.classList.toggle("show", !!on);
}
window.onTgState=(on)=>tgSetBadge(!!on);

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
function _manualUpdate(u){ if(u&&u.url&&api().open_url) api().open_url(u.url); }
let _updArmed=false, _updTimer=null;
function _updDisarm(){
  _updArmed=false; clearTimeout(_updTimer);
  const btn=$("#updateBtn"), txt=$("#updateText"), u=(state&&state.update)||{};
  if(btn){ btn.textContent="Обновить"; btn.classList.remove("armed"); }
  if(txt&&u.version) txt.textContent="Доступна новая версия "+u.version+" — обновись";
}
if($("#updateBtn")) $("#updateBtn").onclick=async()=>{
  const u=(state&&state.update)||{}, btn=$("#updateBtn"), txt=$("#updateText");
  // 1-й клик — предупреждаем, что приложение закроется; 2-й (подтверждение) — ставим.
  if(!_updArmed){
    _updArmed=true;
    if(txt) txt.textContent="Приложение закроется, обновится и откроется само.";
    btn.textContent="Закрыть и обновить"; btn.classList.add("armed");
    _updTimer=setTimeout(_updDisarm, 6000);
    return;
  }
  clearTimeout(_updTimer); _updArmed=false; btn.classList.remove("armed");
  btn.disabled=true; btn.textContent="Обновляю…";
  if(txt) txt.textContent="Скачиваю обновление… приложение сейчас закроется.";
  try{
    const r = api().install_update ? await api().install_update() : null;
    if(r && r.ok){ return; }          // пойдёт установка и перезапуск — оставляем "Обновляю…"
    _manualUpdate(u);                 // нет тихого пути — ручное скачивание
  }catch(e){ _manualUpdate(u); }
  btn.disabled=false; btn.textContent="Обновить";
};
// Тихое обновление не удалось (сеть/запуск) — предлагаем ручное скачивание.
window.onUpdateError=(msg)=>{
  const btn=$("#updateBtn"); if(btn){ btn.disabled=false; btn.textContent="Обновить"; }
  _manualUpdate((state&&state.update)||{});
};
if($("#updateClose")) $("#updateClose").onclick=()=>{ _updateDismissed=true; $("#updateBanner").hidden=true; };
if($("#checkUpdate")) $("#checkUpdate").onclick=async()=>{
  const b=$("#checkUpdate");
  // Если проверка уже нашла обновление — второй клик СТАВИТ его прямо из настроек
  // (баннер сверху перекрыт окном настроек, поэтому даём действие здесь же).
  if(b.dataset.mode==="update"){
    b.disabled=true; b.textContent="Обновляю…";
    try{
      const r = api().install_update ? await api().install_update() : null;
      if(r && r.ok) return;                        // пойдёт тихая установка + перезапуск
      _manualUpdate((state&&state.update)||{});     // нет тихого пути — открыть ссылку
    }catch(e){ _manualUpdate((state&&state.update)||{}); }
    b.disabled=false; return;
  }
  const o=b.textContent; b.disabled=true; b.textContent="Проверяю…";
  try{
    const u = api().check_app_update ? await api().check_app_update() : null;
    if(u){ if(state) state.update=u; renderUpdate();
      if(u.available){                             // нашли — превращаем кнопку в действие
        b.dataset.mode="update";
        b.textContent="Обновить до "+(u.version||"новой");
        b.disabled=false; return;
      }
      b.textContent="Актуальная версия";
    }else b.textContent=o;
  }catch(e){ b.textContent=o; }
  b.dataset.mode="";
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
