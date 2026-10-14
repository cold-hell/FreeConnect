/* Эффекты FreeConnect кнопки питания:
   surge — всплеск энергии при включении, discharge — плавная разрядка при выключении.
   Плавные светящиеся частицы (additive), в едином языке со «стеклянным» UI.
   Пиксельными остаются только логотип-молния (logoBolt) и кузница глубокого поиска. */
window.FX = (function(){
  const CELL = 7;                 // размер «пикселя»
  let canvas, ctx, W, H, raf = 0;

  function ensure(){
    if(canvas) return;
    canvas = document.getElementById("fx");
    ctx = canvas.getContext("2d");
    resize();
    window.addEventListener("resize", resize);
  }
  function resize(){
    W = canvas.width = window.innerWidth;
    H = canvas.height = window.innerHeight;
    ctx.imageSmoothingEnabled = false;
  }
  function px(x, y, color, size){
    const s = (size||1)*CELL;
    ctx.fillStyle = color;
    ctx.fillRect(Math.round(x/CELL)*CELL, Math.round(y/CELL)*CELL, s, s);
  }

  // ---------- Плавные помощники (additive-свечение) ----------
  // Мягкая светящаяся точка: радиальный градиент от цвета к прозрачному.
  function glowDot(x, y, r, color, alpha){
    if(r <= 0 || alpha <= 0) return;
    ctx.globalAlpha = Math.min(1, alpha);
    const g = ctx.createRadialGradient(x, y, 0, x, y, r);
    g.addColorStop(0, color);
    g.addColorStop(0.4, color);
    g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI*2);
    ctx.fill();
  }
  // Тонкая светящаяся дуга энергии (сглаженная ломаная со свечением).
  function energyArc(x0, y0, x1, y1, spread, color, width, alpha){
    const dist = Math.hypot(x1-x0, y1-y0) || 1;
    const segs = Math.max(4, Math.round(dist/26));
    const nx = -(y1-y0)/dist, ny = (x1-x0)/dist;   // единичная нормаль
    ctx.save();
    ctx.globalAlpha = Math.min(1, alpha);
    ctx.strokeStyle = color; ctx.lineWidth = width;
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    ctx.shadowColor = color; ctx.shadowBlur = 12;
    ctx.beginPath(); ctx.moveTo(x0, y0);
    for(let i=1;i<=segs;i++){
      const t = i/segs;
      const off = (Math.random()*2-1) * spread * Math.sin(t*Math.PI);   // тоньше к концам
      ctx.lineTo(x0 + (x1-x0)*t + nx*off, y0 + (y1-y0)*t + ny*off);
    }
    ctx.stroke();
    ctx.restore();
  }

  // ---------- Всплеск энергии (включение) ----------
  function surge(cx, cy){
    ensure(); cancelAnimationFrame(raf);
    const start = performance.now(), dur = 820;
    const parts = [];
    for(let i=0;i<46;i++){
      parts.push({
        a: Math.random()*Math.PI*2,
        sp: 55 + Math.random()*220,              // px/сек
        r0: 7 + Math.random()*10,
        born: Math.random()*0.12,
        col: Math.random()>0.45 ? "rgba(120,240,255,1)" : "rgba(143,176,255,1)"
      });
    }
    function frame(now){
      const t = (now-start)/dur;
      ctx.clearRect(0,0,W,H);
      if(t>=1){ ctx.globalAlpha=1; ctx.globalCompositeOperation="source-over"; return; }
      ctx.globalCompositeOperation = "lighter";
      // центральный bloom, всплывает и тает
      const bloomR = 26 + t*120;
      glowDot(cx, cy, bloomR,     "rgba(90,230,220,1)",  (1-t)*0.45);
      glowDot(cx, cy, bloomR*0.5, "rgba(200,255,250,1)", (1-t)*0.55);
      // разлёт частиц (ease-out наружу, сжимаются и гаснут)
      for(const p of parts){
        const lt = (t - p.born)/(1-p.born);
        if(lt <= 0) continue;
        const ease = 1-(1-lt)*(1-lt);
        const d = p.sp * ease * (dur/1000);
        glowDot(cx + Math.cos(p.a)*d, cy + Math.sin(p.a)*d,
                p.r0*(1-lt*0.6), p.col, (1-lt)*0.9);
      }
      // дуги заряда бьют в кнопку сверху (только в начале, мерцают)
      if(t < 0.42 && (Math.floor(now/45)%2===0)){
        for(let b=0;b<3;b++){
          const sx = cx + (Math.random()*160-80);
          energyArc(sx, cy-175, cx, cy, 24, "rgba(200,252,255,1)", 2, (1-t/0.42)*0.85);
        }
      }
      raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);
  }

  // ---------- Разрядка / power-down (выключение) ----------
  // Свет стягивается к центру и гаснет — «отключился», без огня.
  function discharge(cx, cy){
    ensure(); cancelAnimationFrame(raf);
    const start = performance.now(), dur = 820;
    const parts = [];
    for(let i=0;i<40;i++){
      parts.push({
        a: Math.random()*Math.PI*2,
        r: 68 + Math.random()*70,                // стартовый радиус
        r0: 6 + Math.random()*8,
        born: Math.random()*0.15,
        // цвет остывает от бирюзы к серому
        col: Math.random()>0.5 ? "rgba(120,240,255,1)" : "rgba(150,165,190,1)"
      });
    }
    function frame(now){
      const t = (now-start)/dur;
      ctx.clearRect(0,0,W,H);
      if(t>=1){ ctx.globalAlpha=1; ctx.globalCompositeOperation="source-over"; return; }
      ctx.globalCompositeOperation = "lighter";
      // частицы ускоряются внутрь и гаснут
      for(const p of parts){
        const lt = (t - p.born)/(1-p.born);
        if(lt <= 0) continue;
        const d = p.r * (1 - lt*lt);              // ease-in к центру
        glowDot(cx + Math.cos(p.a)*d, cy + Math.sin(p.a)*d,
                p.r0*(1-lt*0.7), p.col, (1-lt)*0.7);
      }
      // ядро: короткая вспышка и плавное затухание
      const coreA = t<0.15 ? (t/0.15)*0.5 : (1-(t-0.15)/0.85)*0.5;
      glowDot(cx, cy, 42*(1-t*0.5), "rgba(120,220,215,1)", Math.max(0, coreA));
      raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);
  }

  function clear(){ if(ctx){ cancelAnimationFrame(raf); ctx.globalCompositeOperation="source-over"; ctx.globalAlpha=1; ctx.clearRect(0,0,W,H); } }

  // ---------- Логотип: анимированная пиксельная молния ----------
  function hex(h){ h=h.replace("#",""); return [parseInt(h.slice(0,2),16),parseInt(h.slice(2,4),16),parseInt(h.slice(4,6),16)]; }
  function mix(a,b,t){ const p=hex(a),q=hex(b);
    return `rgb(${Math.round(p[0]+(q[0]-p[0])*t)},${Math.round(p[1]+(q[1]-p[1])*t)},${Math.round(p[2]+(q[2]-p[2])*t)})`; }

  function logoBolt(el){
    if(!el) return;
    const c = el.getContext("2d");
    c.imageSmoothingEnabled = false;
    const cell = 4;
    const rows = [[4,5,6],[3,4,5],[2,3,4],[1,2,3],[1,2,3,4,5,6],[4,5,6],[5,6,7],[6,7],[6,7],[7]];
    const cells = [];
    rows.forEach((cols,r)=>cols.forEach(col=>cells.push([col,r])));
    const RN = rows.length;
    const colorFor = (r)=> r/(RN-1) < 0.5
      ? mix("#37e0c4","#eafffb", (r/(RN-1))*2)
      : mix("#eafffb","#6b8cff", (r/(RN-1)-0.5)*2);
    let last=0, flashUntil=0, nextFlash=performance.now()+1400, sparkStart=-1;
    function frame(now){
      if(now-last>42){                       // ~24 fps достаточно
        last=now;
        c.clearRect(0,0,el.width,el.height);
        if(now>nextFlash){ flashUntil=now+90; nextFlash=now+1400+Math.random()*2200; sparkStart=now; }
        const flash = now<flashUntil;
        let sr=-1;
        if(sparkStart>=0){ sr=Math.floor((now-sparkStart)/40); if(sr>=RN){ sparkStart=-1; sr=-1; } }
        for(const [col,r] of cells){
          c.fillStyle = flash ? "#ffffff" : (r===sr ? "#ffffff" : colorFor(r));
          c.fillRect(col*cell, r*cell, cell, cell);
        }
      }
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  // ---------- Кузница: молот бьёт по наковальне (глубокий поиск) ----------
  let forge = null;
  function forgeStart(canvas){
    if(!canvas) return;
    const ctx = canvas.getContext("2d");
    ctx.imageSmoothingEnabled = false;
    const c = 4, cx = canvas.width/2;
    forge = { canvas, ctx, found:0, parts:[], raf:0, t0:performance.now(),
              prevPhase:0, burst:0 };
    const P = (x,y,col,s)=>{ ctx.fillStyle=col; ctx.fillRect(Math.round(x/c)*c, Math.round(y/c)*c, (s||1)*c, (s||1)*c); };

    const anvilTop = canvas.height*0.55;   // уровень удара
    function drawAnvil(){
      const y = anvilTop;
      // верхняя плита
      for(let x=cx-40;x<cx+40;x+=c) P(x, y, "#4c566a");
      for(let x=cx-40;x<cx+40;x+=c) P(x, y-c, "#737d94");         // блик
      for(let x=cx-40;x<cx+44;x+=c){ P(x, y+c, "#3b4252"); P(x, y+2*c, "#3b4252"); }
      // рог слева
      for(let i=0;i<5;i++) P(cx-44-i*c, y-c+ (i>2?c:0), "#4c566a");
      // талия
      for(let yy=y+3*c; yy<y+9*c; yy+=c){ for(let x=cx-14;x<cx+14;x+=c) P(x, yy, "#434b5c"); }
      // основание
      for(let yy=y+9*c; yy<y+12*c; yy+=c){ for(let x=cx-30;x<cx+30;x+=c) P(x, yy, "#3b4252"); }
    }
    function drawHammer(hy){
      // hy — уровень НИЗА головы молота (ударная грань)
      const headW = 36, headH = 16;
      const hx = cx - headW/2;
      // рукоять — вертикальный столб вверх из центра головы
      for(let i=1;i<=13;i++){
        const y = hy - headH - i*c;
        P(cx-c, y, "#7a4a24"); P(cx, y, "#8a5a2c");
      }
      // голова молота (сплошной прямоугольник)
      for(let x=hx; x<hx+headW; x+=c)
        for(let y=hy-headH; y<hy; y+=c) P(x, y, "#5b6478");
      // объём: блики и тени
      for(let x=hx; x<hx+headW; x+=c) P(x, hy-headH, "#8892a6");     // верхняя грань
      for(let y=hy-headH; y<hy; y+=c) P(hx, y, "#8892a6");           // левый блик
      for(let x=hx; x<hx+headW; x+=c) P(x, hy-c, "#3f4757");         // ударная грань (тёмная)
      for(let y=hy-headH; y<hy; y+=c) P(hx+headW-c, y, "#3f4757");   // правая тень
    }
    function spawnImpact(){
      const n = 8 + Math.floor(Math.random()*5);
      for(let i=0;i<n;i++){
        const a = -Math.PI/2 + (Math.random()-0.5)*2.2;
        const sp = 1.2 + Math.random()*2.2;
        forge.parts.push({x:cx, y:anvilTop-2, vx:Math.cos(a)*sp*c*0.4, vy:Math.sin(a)*sp*c*0.4,
                          life:0, max:0.4+Math.random()*0.4, kind:"spark"});
      }
      // Если стратегии уже создаются — вместо искр летят молнии, чем больше тем больше
      const bolts = Math.min(forge.found, 9) + (forge.burst>0 ? 5 : 0);
      for(let i=0;i<bolts;i++){
        const a = -Math.PI/2 + (Math.random()-0.5)*2.6;
        const sp = 2 + Math.random()*2.5;
        forge.parts.push({x:cx, y:anvilTop-2, vx:Math.cos(a)*sp*c*0.5, vy:Math.sin(a)*sp*c*0.5,
                          life:0, max:0.5+Math.random()*0.4, kind:"bolt"});
      }
      if(forge.burst>0) forge.burst--;
    }
    function drawParts(){
      for(const p of forge.parts){
        if(p.life>=1) continue;
        p.life += 0.016/p.max; p.x+=p.vx; p.y+=p.vy;
        if(p.kind==="spark"){ p.vy += 0.09*c; const col = p.life<0.5?"#ffe08a":"#ff8c1a"; P(p.x,p.y,col); }
        else { // молния: короткий зигзаг
          const col = p.life<0.5?"#eafffb":"#37e0c4";
          P(p.x, p.y, col); P(p.x + (Math.random()>.5?c:-c), p.y+c, "rgba(107,140,255,.8)");
        }
      }
      forge.parts = forge.parts.filter(p=>p.life<1);
    }
    function frame(now){
      const period = Math.max(300, 720 - forge.found*70);   // чем больше найдено — тем быстрее
      const phase = ((now - forge.t0) % period) / period;
      // удар при переходе через 0.4
      if(forge.prevPhase < 0.4 && phase >= 0.4) spawnImpact();
      forge.prevPhase = phase;
      // позиция молота (hy — низ головы; при ударе почти касается наковальни)
      const raised = 18, hit = anvilTop - 4;
      let hy;
      if(phase < 0.4){ const t = phase/0.4; hy = raised + (hit-raised)*(t*t); }
      else { const t = (phase-0.4)/0.6; hy = hit + (raised-hit)*(1-(1-t)*(1-t)); }
      ctx.clearRect(0,0,canvas.width,canvas.height);
      if(forge.burst>0){ ctx.fillStyle="rgba(120,240,255,.10)"; ctx.fillRect(0,0,canvas.width,canvas.height); }
      drawAnvil();
      drawHammer(hy);
      drawParts();
      forge.raf = requestAnimationFrame(frame);
    }
    forge.raf = requestAnimationFrame(frame);
  }
  function forgeSetFound(n){ if(forge) forge.found = n; }
  function forgeBurst(){ if(forge) forge.burst = 3; }
  function forgeStop(){ if(forge){ cancelAnimationFrame(forge.raf); forge.ctx.clearRect(0,0,forge.canvas.width,forge.canvas.height); forge=null; } }

  return { surge, discharge, lightning:surge, fire:discharge, clear,
           logoBolt, forgeStart, forgeSetFound, forgeBurst, forgeStop };
})();
