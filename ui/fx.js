/* Пиксельные эффекты FreeConnect: молнии при включении, пожар при выключении.
   Рендер на canvas с отключённым сглаживанием — нарочито «пиксельный» вид. */
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

  // ---------- Молнии (включение) ----------
  function drawBolt(x0, y0, x1, y1){
    const steps = Math.max(6, Math.abs(y1-y0)/CELL);
    const dx = (x1-x0)/steps;
    let x = x0, y = y0;
    for(let i=0;i<steps;i++){
      x += dx + (Math.random()*3-1.5)*CELL;
      y += (y1-y0)/steps;
      px(x-CELL, y, "rgba(55,224,196,.5)");
      px(x+CELL, y, "rgba(107,140,255,.5)");
      px(x, y, "#eafffb");                       // белое ядро
      if(Math.random()<0.12){                    // ответвление
        let bx = x;
        for(let j=0;j<3;j++){ bx += (Math.random()>.5?CELL:-CELL); px(bx, y+j*CELL, "rgba(191,249,255,.75)"); }
      }
    }
  }
  function lightning(cx, cy){
    ensure(); cancelAnimationFrame(raf);
    const start = performance.now(), dur = 750;
    function frame(now){
      const t = (now-start)/dur;
      ctx.clearRect(0,0,W,H);
      if(t>=1){ return; }
      const flash = Math.max(0, 1 - t*3);
      if(flash>0){ ctx.fillStyle = `rgba(120,240,255,${0.22*flash})`; ctx.fillRect(0,0,W,H); }
      const on = (Math.floor(now/55)%2===0) || t<0.15;
      if(on){
        for(let b=0;b<3;b++) drawBolt(cx+(b-1)*42, -10, cx+(Math.random()*60-30), cy);
        for(let s=0;s<12;s++){
          const a=Math.random()*Math.PI*2, r=40+Math.random()*75;
          px(cx+Math.cos(a)*r, cy+Math.sin(a)*r, Math.random()>.5?"#bff9ff":"#37e0c4");
        }
      }
      raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);
  }

  // ---------- Пожар (выключение) ----------
  function fire(cx, cy){
    ensure(); cancelAnimationFrame(raf);
    const start = performance.now(), dur = 1050;
    const parts = [];
    const colors = ["#fff3b0","#ffd23f","#ff8c1a","#ff4d1a","#c81f1f"];
    function frame(now){
      const t = (now-start)/dur;
      ctx.clearRect(0,0,W,H);
      if(t>=1){ return; }
      if(t<0.55){                                // эмиссия частиц
        for(let i=0;i<9;i++){
          const a=Math.random()*Math.PI*2, r=Math.random()*72;
          parts.push({
            x: cx+Math.cos(a)*r,
            y: cy+Math.abs(Math.sin(a))*28+18,
            vx:(Math.random()*2-1)*CELL*0.18,
            vy:-(1+Math.random()*2.4)*CELL*0.42,
            life:0, max:0.5+Math.random()*0.5,
            big:Math.random()<0.25
          });
        }
      }
      for(const p of parts){
        if(p.life>=1) continue;
        p.life += 0.016/p.max; p.x+=p.vx; p.y+=p.vy; p.vy*=0.985;
        const ci = Math.min(colors.length-1, Math.floor(p.life*colors.length));
        px(p.x, p.y, colors[ci], p.big?2:1);
      }
      raf = requestAnimationFrame(frame);
    }
    raf = requestAnimationFrame(frame);
  }

  function clear(){ if(ctx){ cancelAnimationFrame(raf); ctx.clearRect(0,0,W,H); } }

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

  return { lightning, fire, clear, logoBolt, forgeStart, forgeSetFound, forgeBurst, forgeStop };
})();
