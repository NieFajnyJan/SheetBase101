// Collections – a small first-person shooter in raw concrete, drawn by raycasting on a 480×270 canvas.
// The enemies are the player's open debts (from the page, #game-enemies); stamping one PAID only happens
// on screen – the game never sends anything back. Original code and drawings; no images or libraries.
(() => {
  "use strict";
  const canvas = document.getElementById("game");
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height, HALF = H / 2;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- The level. 1–3: kinds of concrete wall, 4: a wall with a red hazard band. "." is open floor.
  const MAP = [
    "111111111111111111111111",
    "1......2.......1.......1",
    "1.11...2...3...1..222..1",
    "1.1....2.......1.......1",
    "1.1..111141.1111...3...1",
    "1......................1",
    "1111.11.2222222.11.11111",
    "1......2.......2.......1",
    "1..3...2...1...2...3...1",
    "1......2.......4.......1",
    "1.11111......111111.1..1",
    "1...........3..........1",
    "1..222..111111111..22..1",
    "1......................1",
    "1...3......1.......3...1",
    "111111111111111111111111",
  ];
  const cell = (x, y) => (MAP[y | 0] || "")[x | 0] || "1";
  const isWall = (x, y) => cell(x, y) !== ".";

  // ---- Textures, drawn once into small canvases.
  const T = 64;
  const rand = seed => () => ((seed = (seed * 16807) % 2147483647) - 1) / 2147483646;

  function concrete(kind) {
    const c = document.createElement("canvas");
    c.width = c.height = T;
    const g = c.getContext("2d");
    const r = rand(kind * 977 + 13);
    const base = [0, 92, 78, 104, 86][kind];
    const img = g.createImageData(T, T);
    for (let y = 0; y < T; y++) {
      for (let x = 0; x < T; x++) {
        let v = base + (r() - .5) * 34;            // aggregate
        if (y % 16 === 15) v -= 34;                // formwork board joints
        if (y % 16 === 0) v += 10;
        const i = (y * T + x) * 4;
        img.data[i] = v; img.data[i + 1] = v; img.data[i + 2] = v - 4; img.data[i + 3] = 255;
      }
    }
    g.putImageData(img, 0, 0);
    g.fillStyle = "rgba(0,0,0,.35)";               // stains running down
    for (let i = 0; i < 5; i++) g.fillRect(r() * T | 0, 0, 1 + r() * 3 | 0, 20 + r() * 44);
    g.fillStyle = "#050505";                       // form-tie holes
    for (const [hx, hy] of [[16, 8], [48, 8], [16, 40], [48, 40]]) g.fillRect(hx - 1, hy - 1, 3, 3);
    if (kind === 4) {                              // hazard band
      for (let x = -T; x < T; x += 8) {
        g.fillStyle = (x / 8) % 2 ? "#d61f0a" : "#111";
        g.beginPath(); g.moveTo(x, 34); g.lineTo(x + 8, 34); g.lineTo(x + 16, 26); g.lineTo(x + 8, 26); g.fill();
      }
    }
    return c;
  }
  const WALLS = [null, concrete(1), concrete(2), concrete(3), concrete(4)];

  // A debt: a floating invoice with teeth, eyes and a red stamp. Hurt: drawn white-hot.
  function invoice(hurt) {
    const c = document.createElement("canvas");
    c.width = c.height = T;
    const g = c.getContext("2d");
    g.fillStyle = hurt ? "#ffffff" : "#d8d4c4";
    g.beginPath();
    g.moveTo(14, 4); g.lineTo(50, 4); g.lineTo(54, 50);
    for (let x = 54; x > 10; x -= 6) { g.lineTo(x - 3, 58); g.lineTo(x - 6, 50); }   // torn bottom edge
    g.closePath(); g.fill();
    g.fillStyle = "#6b685c";
    for (let y = 12; y < 30; y += 5) g.fillRect(19, y, 22 - (y % 3) * 3, 2);         // the small print
    g.fillStyle = "#000";
    g.fillRect(20, 36, 24, 8);                                                       // mouth
    g.fillStyle = hurt ? "#000" : "#d8d4c4";
    for (let x = 20; x < 44; x += 4) { g.fillRect(x, 36, 2, 3); g.fillRect(x + 2, 41, 2, 3); }   // teeth
    g.fillStyle = "#ff2a00";
    g.fillRect(21, 30, 6, 4); g.fillRect(37, 30, 6, 4);                              // eyes
    g.strokeStyle = "#c4130a"; g.lineWidth = 2;
    g.beginPath(); g.arc(44, 15, 7, 0, Math.PI * 2); g.stroke();                     // stamp
    g.fillStyle = "#c4130a"; g.font = "bold 7px monospace"; g.textAlign = "center"; g.fillText("zł", 44, 18);
    return c;
  }
  const ENEMY = invoice(false), ENEMY_HURT = invoice(true);

  // ---- Sound: short synthesised blips, only after the first click (browsers require that).
  let audio = null, muted = false;
  function sound(kind) {
    if (muted) return;
    try {
      audio = audio || new AudioContext();
      const t = audio.currentTime, o = audio.createOscillator(), v = audio.createGain();
      const [type, from, to, len, vol] = {
        shoot: ["square", 220, 40, .12, .08], hit: ["sawtooth", 140, 60, .1, .07],
        paid: ["square", 330, 660, .25, .06], hurt: ["triangle", 90, 50, .2, .12],
      }[kind];
      o.type = type; o.frequency.setValueAtTime(from, t); o.frequency.exponentialRampToValueAtTime(to, t + len);
      v.gain.setValueAtTime(vol, t); v.gain.exponentialRampToValueAtTime(.0001, t + len);
      o.connect(v).connect(audio.destination); o.start(t); o.stop(t + len);
    } catch (e) { /* no sound, still a game */ }
  }

  // ---- The debts on the page; padded with made-up ones so there's always something to fight.
  let real = [];
  try { real = JSON.parse(document.getElementById("game-enemies").textContent) || []; } catch (e) {}
  const MADE_UP = ["Late fee", "Interest", "Overdraft", "Penalty", "Arrears", "Service charge", "Stamp duty", "Surcharge"];

  let player, enemies, state, stamped, flash, shake, recoil, cooldown, message, lastTime;

  function emptyCellFarFrom(px, py, taken) {
    for (let tries = 0; tries < 500; tries++) {
      const x = 1 + Math.random() * (MAP[0].length - 2) | 0, y = 1 + Math.random() * (MAP.length - 2) | 0;
      const key = `${x},${y}`;
      if (!isWall(x, y) && !taken.has(key) && Math.hypot(x - px, y - py) > 6) { taken.add(key); return [x + .5, y + .5]; }
    }
    return [MAP[0].length - 2.5, MAP.length - 2.5];
  }

  function reset() {
    player = { x: 1.5, y: 1.5, dir: 0, hp: 100 };
    const list = real.map(d => ({ ...d, made: false }));
    while (list.length < 8) {
      const value = Math.round(5 + Math.random() * 195);
      list.push({ what: MADE_UP[list.length % MADE_UP.length], amount: `${value},00 zł`, value, made: true });
    }
    const taken = new Set();
    enemies = list.map(d => {
      const [x, y] = emptyCellFarFrom(player.x, player.y, taken);
      return { ...d, x, y, hp: 3, hurt: 0, dead: 0, bob: Math.random() * 6 };
    });
    stamped = 0; flash = 0; shake = 0; recoil = 0; cooldown = 0;
    message = real.length ? "" : "You owe nobody anything – these debts are made up.";
  }

  // ---- Input
  const keys = new Set();
  let mouseTurn = 0;
  const locked = () => document.pointerLockElement === canvas;

  function start() {
    if (state === "won" || state === "lost") reset();
    state = "play";
    lastTime = performance.now();
    canvas.focus();
    if (canvas.requestPointerLock) canvas.requestPointerLock();
  }
  function stop() {
    if (state === "play") state = "paused";
    keys.clear();
  }
  canvas.addEventListener("click", () => (state === "play" ? fire() : start()));
  document.addEventListener("pointerlockchange", () => { if (!locked()) stop(); });
  document.addEventListener("mousemove", e => { if (locked()) mouseTurn += e.movementX; });
  canvas.addEventListener("keydown", e => {
    if (e.key === "m" || e.key === "M") { muted = !muted; return; }
    if (e.key === "Escape") { stop(); return; }
    if (state !== "play") { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); start(); } return; }
    if (e.key === " ") { e.preventDefault(); fire(); return; }
    if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key)) e.preventDefault();
    keys.add(e.key.toLowerCase());
  });
  canvas.addEventListener("keyup", e => keys.delete(e.key.toLowerCase()));
  canvas.addEventListener("blur", stop);
  window.addEventListener("blur", stop);

  // ---- Firing: whatever is in the middle of the screen and nearer than the wall behind it.
  let centreHit = null;   // set while drawing: the nearest enemy under the crosshair
  function fire() {
    if (cooldown > 0 || state !== "play") return;
    cooldown = .28; recoil = 1; flash = Math.max(flash, .06);
    sound("shoot");
    const e = centreHit;
    if (!e) return;
    e.hp -= 1; e.hurt = .12;
    const away = Math.atan2(e.y - player.y, e.x - player.x);   // knocked back a little
    const nx = e.x + Math.cos(away) * .25, ny = e.y + Math.sin(away) * .25;
    if (!isWall(nx, ny)) { e.x = nx; e.y = ny; }
    if (e.hp <= 0) {
      e.dead = 1.4; stamped += 1; sound("paid");
      if (enemies.every(x => x.dead)) setTimeout(() => { if (state === "play") { state = "won"; document.exitPointerLock?.(); } }, 900);
    } else sound("hit");
  }

  // ---- Movement and the debts closing in.
  function sees(e) {   // nothing solid between the debt and the player
    const steps = Math.ceil(Math.hypot(player.x - e.x, player.y - e.y) * 4);
    for (let i = 1; i < steps; i++) {
      const t = i / steps;
      if (isWall(e.x + (player.x - e.x) * t, e.y + (player.y - e.y) * t)) return false;
    }
    return true;
  }
  function tryMove(o, dx, dy, r) {
    if (!isWall(o.x + dx + Math.sign(dx) * r, o.y)) o.x += dx;
    if (!isWall(o.x, o.y + dy + Math.sign(dy) * r)) o.y += dy;
  }

  function update(dt) {
    const turn = (keys.has("arrowleft") ? -1 : 0) + (keys.has("arrowright") ? 1 : 0);
    player.dir += turn * 2.6 * dt + mouseTurn * .0026;
    mouseTurn = 0;
    const fwd = (keys.has("w") || keys.has("arrowup") ? 1 : 0) - (keys.has("s") || keys.has("arrowdown") ? 1 : 0);
    const side = (keys.has("d") ? 1 : 0) - (keys.has("a") ? 1 : 0);
    const cos = Math.cos(player.dir), sin = Math.sin(player.dir), speed = 3.2 * dt;
    tryMove(player, (cos * fwd - sin * side) * speed, (sin * fwd + cos * side) * speed, .22);

    for (const e of enemies) {
      e.bob += dt * 3;
      if (e.hurt > 0) e.hurt -= dt;
      if (e.dead) { e.dead = Math.max(0, e.dead - dt) || -1; continue; }   // -1: gone for good
      const dx = player.x - e.x, dy = player.y - e.y, d = Math.hypot(dx, dy);
      if (d < 14 && d > .6 && sees(e)) tryMove(e, dx / d * 1.35 * dt, dy / d * 1.35 * dt, .25);
      if (d < .8) {
        player.hp -= 22 * dt;
        if (flash < .25) { flash = .25; shake = reducedMotion ? 0 : .2; sound("hurt"); }
      }
    }
    if (player.hp <= 0) { player.hp = 0; state = "lost"; document.exitPointerLock?.(); }
    cooldown = Math.max(0, cooldown - dt);
    recoil = Math.max(0, recoil - dt * 6);
    flash = Math.max(0, flash - dt);
    shake = Math.max(0, shake - dt);
  }

  // ---- Drawing
  const FOV = .66;   // the camera plane's half-width
  const zbuf = new Float32Array(W);

  function draw() {
    const ox = shake ? (Math.random() - .5) * 6 : 0, oy = shake ? (Math.random() - .5) * 6 : 0;
    ctx.save();
    ctx.translate(ox, oy);

    // Ceiling and floor: dark slabs fading into the dark.
    let g = ctx.createLinearGradient(0, 0, 0, HALF);
    g.addColorStop(0, "#1e1d1b"); g.addColorStop(1, "#050505");
    ctx.fillStyle = g; ctx.fillRect(-8, -8, W + 16, HALF + 8);
    g = ctx.createLinearGradient(0, HALF, 0, H);
    g.addColorStop(0, "#040404"); g.addColorStop(1, "#2a2926");
    ctx.fillStyle = g; ctx.fillRect(-8, HALF, W + 16, HALF + 8);

    const dirX = Math.cos(player.dir), dirY = Math.sin(player.dir);
    const planeX = -dirY * FOV, planeY = dirX * FOV;

    // Walls: one ray per column (DDA through the grid).
    for (let x = 0; x < W; x++) {
      const camX = 2 * x / W - 1;
      const rx = dirX + planeX * camX, ry = dirY + planeY * camX;
      let mx = player.x | 0, my = player.y | 0;
      const ddx = Math.abs(1 / rx), ddy = Math.abs(1 / ry);
      const sx = rx < 0 ? -1 : 1, sy = ry < 0 ? -1 : 1;
      let sdx = (rx < 0 ? player.x - mx : mx + 1 - player.x) * ddx;
      let sdy = (ry < 0 ? player.y - my : my + 1 - player.y) * ddy;
      let ns = 0, kind = 1;
      for (let i = 0; i < 64; i++) {
        if (sdx < sdy) { sdx += ddx; mx += sx; ns = 0; } else { sdy += ddy; my += sy; ns = 1; }
        const c = cell(mx, my);
        if (c !== ".") { kind = +c || 1; break; }
      }
      const dist = Math.max(.0001, ns ? sdy - ddy : sdx - ddx);
      zbuf[x] = dist;
      const h = H / dist | 0;
      let wallX = ns ? player.x + dist * rx : player.y + dist * ry;
      wallX -= Math.floor(wallX);
      let tx = wallX * T | 0;
      if ((!ns && rx > 0) || (ns && ry < 0)) tx = T - tx - 1;
      ctx.drawImage(WALLS[kind], tx, 0, 1, T, x, HALF - h / 2, 1, h);
      const dark = Math.min(.92, dist / 11 + (ns ? .22 : 0));   // fog, and the sides in shadow
      ctx.fillStyle = `rgba(0,0,0,${dark})`;
      ctx.fillRect(x, HALF - h / 2, 1, h);
    }

    // Debts: billboards, far ones first, cut off where a wall is nearer.
    centreHit = null;
    const inv = 1 / (planeX * dirY - dirX * planeY);
    const visible = enemies.filter(e => e.dead >= 0)
      .map(e => ({ e, d: (player.x - e.x) ** 2 + (player.y - e.y) ** 2 }))
      .sort((a, b) => b.d - a.d);
    for (const { e } of visible) {
      const rx = e.x - player.x, ry = e.y - player.y;
      const tX = inv * (dirY * rx - dirX * ry), tY = inv * (-planeY * rx + planeX * ry);
      if (tY <= .2) continue;
      const screenX = (W / 2) * (1 + tX / tY);
      const size = Math.abs(H / tY) * .8 | 0;
      const top = HALF - size / 2 + (e.dead ? 0 : Math.sin(e.bob) * size * .06) + size * .1;
      const left = screenX - size / 2 | 0;
      const shade = Math.max(.18, 1 - tY / 12);
      if (e.dead) {   // stamped: a red PAID where it was
        ctx.globalAlpha = Math.min(1, e.dead);
        ctx.fillStyle = "#e01b00";
        ctx.font = `bold ${Math.max(8, size / 4 | 0)}px Impact, monospace`;
        ctx.textAlign = "center";
        if (zbuf[Math.max(0, Math.min(W - 1, screenX | 0))] > tY) ctx.fillText("PAID", screenX, HALF);
        ctx.globalAlpha = 1;
        continue;
      }
      ctx.filter = `brightness(${shade})`;
      const img = e.hurt > 0 ? ENEMY_HURT : ENEMY;
      for (let x = Math.max(0, left); x < Math.min(W, left + size); x++) {
        if (zbuf[x] <= tY) continue;
        const tx = (x - left) * T / size | 0;
        ctx.drawImage(img, tx, 0, 1, T, x, top, 1, size);
      }
      ctx.filter = "none";
      if (left <= W / 2 && W / 2 < left + size && zbuf[W / 2 | 0] > tY) centreHit = e;   // nearest wins (drawn last)
      if (tY < 7 && zbuf[Math.max(0, Math.min(W - 1, screenX | 0))] > tY) {   // what it is, when close
        ctx.font = "bold 9px monospace"; ctx.textAlign = "center";
        const label = `${e.what} · ${e.amount}`;
        const w = ctx.measureText(label).width + 6;
        ctx.fillStyle = "rgba(0,0,0,.8)"; ctx.fillRect(screenX - w / 2, top - 14, w, 12);
        ctx.fillStyle = e.made ? "#aeaba1" : "#ffd400"; ctx.fillText(label, screenX, top - 5);
      }
    }

    // The stamp gun: a concrete-grey block with a red PAID head, kicking back when fired.
    const kick = recoil * 14, bobY = state === "play" && keys.size ? Math.sin(performance.now() / 110) * 2 : 0;
    const gx = W / 2 - 34, gy = H - 64 + kick + bobY;
    ctx.fillStyle = "#000"; ctx.fillRect(gx + 22, gy + 30, 24, 40);                   // grip
    ctx.fillStyle = "#5a5953"; ctx.fillRect(gx, gy + 8, 68, 30);                       // body
    ctx.fillStyle = "#3a3935"; ctx.fillRect(gx, gy + 30, 68, 8);
    ctx.fillStyle = "#c4130a"; ctx.fillRect(gx + 14, gy - 6, 40, 16);                  // stamp head
    ctx.fillStyle = "#000"; ctx.font = "bold 10px Impact, monospace"; ctx.textAlign = "center";
    ctx.fillText("PAID", gx + 34, gy + 6);
    if (recoil > .6) {                                                                  // muzzle flash
      ctx.fillStyle = "#ffd400";
      ctx.beginPath(); ctx.moveTo(W / 2, gy - 30); ctx.lineTo(W / 2 - 14, gy - 6); ctx.lineTo(W / 2 + 14, gy - 6); ctx.fill();
    }

    // Crosshair: brackets that go red over a debt.
    ctx.strokeStyle = centreHit ? "#ff2a00" : "#ecebe4"; ctx.lineWidth = 1;
    ctx.strokeRect(W / 2 - 4.5, HALF - 4.5, 9, 9);
    ctx.restore();

    // Getting hit (or firing): the screen flashes.
    if (flash > 0) { ctx.fillStyle = `rgba(200,20,0,${Math.min(.5, flash)})`; ctx.fillRect(0, 0, W, H); }

    // HUD: a black concrete strip along the bottom.
    ctx.fillStyle = "#000"; ctx.fillRect(0, H - 16, W, 16);
    ctx.font = "bold 10px monospace"; ctx.textAlign = "left";
    ctx.fillStyle = player.hp > 30 ? "#ecebe4" : "#ff2a00";
    ctx.fillText(`HEALTH ${Math.ceil(player.hp)}`, 6, H - 5);
    ctx.fillStyle = "#ecebe4"; ctx.textAlign = "center";
    ctx.fillText(`DEBTS LEFT ${enemies.length - stamped}`, W / 2, H - 5);
    ctx.textAlign = "right"; ctx.fillStyle = "#ffd400";
    ctx.fillText(`STAMPED ${stamped}/${enemies.length}${muted ? " · MUTED" : ""}`, W - 6, H - 5);
    minimap();

    if (state !== "play") overlay();
  }

  function minimap() {
    const s = 3, x0 = W - MAP[0].length * s - 4, y0 = 4;
    ctx.fillStyle = "rgba(0,0,0,.6)"; ctx.fillRect(x0 - 2, y0 - 2, MAP[0].length * s + 4, MAP.length * s + 4);
    for (let y = 0; y < MAP.length; y++) for (let x = 0; x < MAP[0].length; x++) {
      if (isWall(x, y)) { ctx.fillStyle = cell(x, y) === "4" ? "#c4130a" : "#55544e"; ctx.fillRect(x0 + x * s, y0 + y * s, s, s); }
    }
    ctx.fillStyle = "#ff2a00";
    for (const e of enemies) if (!e.dead) ctx.fillRect(x0 + e.x * s - 1, y0 + e.y * s - 1, 2, 2);
    ctx.fillStyle = "#ffd400"; ctx.fillRect(x0 + player.x * s - 1, y0 + player.y * s - 1, 3, 3);
  }

  function overlay() {
    ctx.fillStyle = "rgba(0,0,0,.72)"; ctx.fillRect(0, 0, W, H - 16);
    ctx.textAlign = "center";
    const title = { title: "COLLECTIONS", paused: "PAUSED", won: "ALL DEBTS STAMPED", lost: "BURIED IN DEBT" }[state];
    const line = {
      title: `${enemies.length} debts loose in the concrete. Stamp them all PAID.`,
      paused: "Click to carry on.",
      won: "Only in here, though: your real debts are still on the Debts page.",
      lost: `They got you. ${stamped} of ${enemies.length} stamped.`,
    }[state];
    ctx.fillStyle = state === "lost" ? "#ff2a00" : state === "won" ? "#ffd400" : "#ecebe4";
    ctx.font = "900 40px Impact, 'Arial Narrow', 'Nimbus Sans Narrow', 'Arial Black', sans-serif";
    ctx.fillText(title, W / 2, HALF - 20);
    ctx.fillStyle = "#ecebe4"; ctx.font = "bold 10px monospace";
    ctx.fillText(line, W / 2, HALF + 6);
    if (message && state === "title") { ctx.fillStyle = "#aeaba1"; ctx.fillText(message, W / 2, HALF + 22); }
    ctx.fillStyle = "#ffd400";
    ctx.fillText(state === "won" || state === "lost" ? "Click to play again" : "Click to play", W / 2, HALF + 44);
  }

  function frame(now) {
    const dt = Math.min(.05, (now - (lastTime || now)) / 1000);
    lastTime = now;
    if (state === "play") update(dt);
    draw();
    requestAnimationFrame(frame);
  }

  reset();
  state = "title";
  requestAnimationFrame(frame);
})();
