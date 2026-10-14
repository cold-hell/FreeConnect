/* Мини-игра «динозавр Chrome» на время подбора стратегий.
   Пробел/стрелка вверх/клик — прыжок. Пиксель-арт, под тёмную тему. */
window.Dino = (function () {
  let cvs, ctx, raf = 0, run = false, W = 0, H = 0, dpr = 1;
  const s = 3;                 // размер «пикселя»
  const GROUND_H = 16;         // высота полоски земли снизу
  const GRAV = 2000, JUMPV = 620;
  let groundY, dino, obs, speed, score, best = 0, dead, started;
  let spawnAcc, spawnGap, legAcc, legFrame, gscroll, last;

  const COL = '#aeb9c9';       // цвет как у chrome-динозавра, но под тёмный фон
  // Тело динозавра ('#'=пиксель, 'o'=глаз/пропуск). Ноги рисуем отдельно (анимация).
  const BODY = [
    "          ######",
    "          #o#####",
    "          ######",
    "          ###",
    "          ###",
    "#         ####",
    "##       #####",
    "###     ######",
    "##############",
    "##############",
    " #############",
    "  ###########",
    "   #########",
    "    #######",
    "    ###  ##",
  ];
  const LEGA = ["    ##  #", "    #   #", "    #   ##"];
  const LEGB = ["    #   ##", "    #   #", "    ##  #"];
  const BOTH = ["    #   #", "    #   #", "    ##  ##"];
  const CACT1 = ["  ##  ", "# ## #", "# ## #", "# #### ", "######", "  ##  ", "  ##  ", "  ##  "];
  const CACT2 = ["  ##  ", "# ## #", "# ## #", "# ## #", "######", "  ##  ", "  ##  ", "  ##  ", "  ##  ", "  ##  "];

  const DINO_W = 16 * s, DINO_H = (BODY.length + 3) * s, DINO_X = 34;

  function fit() {
    W = cvs.clientWidth || 420; H = cvs.clientHeight || 150;
    dpr = window.devicePixelRatio || 1;
    cvs.width = W * dpr; cvs.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    groundY = H - GROUND_H;
  }
  function reset() {
    dino = { y: 0, vy: 0, onG: true };
    obs = []; speed = 260; score = 0; dead = false; started = false;
    spawnAcc = 0; spawnGap = 1.1; legAcc = 0; legFrame = 0; gscroll = 0;
  }
  function spawn() {
    const big = Math.random() < 0.4;
    const map = big ? CACT2 : CACT1;
    obs.push({ x: W + 10, map: map, w: map[0].length * s, h: map.length * s });
  }
  function jump() {
    if (dead) { reset(); started = true; return; }
    started = true;
    if (dino.onG) { dino.vy = JUMPV; dino.onG = false; }
  }
  function onKey(e) {
    if (!run) return;
    if (e.code === 'Space' || e.code === 'ArrowUp') { e.preventDefault(); e.stopPropagation(); jump(); }
  }
  function onTap(e) { e.preventDefault(); jump(); }

  function drawMap(map, x, topY) {
    ctx.fillStyle = COL;
    for (let r = 0; r < map.length; r++)
      for (let c = 0; c < map[r].length; c++)
        if (map[r][c] === '#') ctx.fillRect(x + c * s, topY + r * s, s, s);
  }

  function update(dt) {
    if (!started || dead) return;
    speed += dt * 9;                       // плавно ускоряемся
    score += dt * speed * 0.02;
    // прыжок/гравитация
    dino.vy -= GRAV * dt; dino.y += dino.vy * dt;
    if (dino.y <= 0) { dino.y = 0; dino.vy = 0; dino.onG = true; }
    // анимация ног
    legAcc += dt; if (legAcc > 0.09) { legAcc = 0; legFrame ^= 1; }
    gscroll = (gscroll + speed * dt) % 12;
    // препятствия
    spawnAcc += dt;
    const gapNeeded = Math.max(0.62, spawnGap - (speed - 260) / 900);
    if (spawnAcc >= gapNeeded) { spawnAcc = 0; spawnGap = 0.9 + Math.random() * 0.9; spawn(); }
    for (const o of obs) o.x -= speed * dt;
    obs = obs.filter(o => o.x + o.w > -4);
    // столкновение (с небольшим прощением по краям)
    const dTop = groundY - dino.y - DINO_H;
    const dx0 = DINO_X + 3, dx1 = DINO_X + DINO_W - 3, dy1 = groundY - dino.y - 3;
    for (const o of obs) {
      if (dx1 > o.x + 2 && dx0 < o.x + o.w - 2 && dy1 > groundY - o.h + 2 && dTop < groundY) {
        dead = true; best = Math.max(best, Math.floor(score)); break;
      }
    }
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    // земля — пунктир с прокруткой
    ctx.fillStyle = COL;
    ctx.globalAlpha = 0.55;
    for (let x = -12 + (12 - gscroll); x < W; x += 12) ctx.fillRect(x, groundY, 7, 2);
    ctx.globalAlpha = 1;
    // динозавр
    const topY = groundY - dino.y - DINO_H;
    drawMap(BODY, DINO_X, topY);
    const legY = topY + BODY.length * s;
    drawMap(dino.onG ? (legFrame ? LEGA : LEGB) : BOTH, DINO_X, legY);
    // кактусы
    for (const o of obs) drawMap(o.map, o.x, groundY - o.h);
    // счёт
    ctx.fillStyle = COL; ctx.font = '600 12px Consolas, monospace';
    ctx.textAlign = 'right';
    ctx.fillText(String(Math.floor(score)).padStart(5, '0'), W - 8, 16);
    if (best) { ctx.globalAlpha = 0.6; ctx.fillText('HI ' + String(best).padStart(5, '0'), W - 70, 16); ctx.globalAlpha = 1; }
    // подсказки/состояния
    ctx.textAlign = 'center';
    if (!started) { ctx.fillStyle = '#37e0c4'; ctx.font = '600 13px Inter, sans-serif'; ctx.fillText('ПРОБЕЛ — прыжок 🦖', W / 2, H / 2 - 6); }
    else if (dead) {
      ctx.fillStyle = '#ff6b6b'; ctx.font = '700 14px Inter, sans-serif';
      ctx.fillText('Столкнулся!', W / 2, H / 2 - 8);
      ctx.fillStyle = COL; ctx.font = '600 12px Inter, sans-serif';
      ctx.fillText('пробел — заново', W / 2, H / 2 + 12);
    }
  }

  function loop(now) {
    if (!run) return;
    const dt = Math.min(0.05, (now - last) / 1000); last = now;
    update(dt); draw();
    raf = requestAnimationFrame(loop);
  }

  function start(canvas) {
    if (run) return;
    cvs = canvas; ctx = cvs.getContext('2d');
    fit(); reset(); run = true; last = performance.now();
    document.addEventListener('keydown', onKey, true);
    cvs.addEventListener('pointerdown', onTap);
    raf = requestAnimationFrame(loop);
  }
  function stop() {
    run = false; cancelAnimationFrame(raf);
    document.removeEventListener('keydown', onKey, true);
    if (cvs) cvs.removeEventListener('pointerdown', onTap);
  }
  return { start, stop };
})();
