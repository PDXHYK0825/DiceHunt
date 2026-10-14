"use strict";

const $ = id => document.getElementById(id);
const phaseNames = {idle:"等待开始",roll:"等待投骰",rolling:"骰子旋转中",input:"等待移动",thinking:"机器思考中",settling:"结算中",finished:"对局结束",stopping:"正在中止",aborted:"已中止",error:"对局失败"};
const modes = {EASY:"简单模式",NORMAL:"普通模式",HARD:"困难模式"};
const notes = {EASY:"地图信息全部可见，与贪心 AI 对战。",NORMAL:"地图信息全部可见，与 OPT 最优 AI 对战。",HARD:"陷阱初始隐藏，踩中后暴露。AI 拥有完整地图信息。"};
let current = null, lastFrame = -1, animatedDice = -1, busy = false, requestPending = false;
let displayPositions = null, resultShown = null, lastLogs = "";
let requestVersion = 0;
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const animate = () => $("animation").checked && !matchMedia("(prefers-reduced-motion: reduce)").matches;

function toast(title, message) {
  const card = document.createElement("div"); card.className = "toast";
  const heading = document.createElement("strong"); heading.textContent = title;
  const body = document.createElement("p"); body.textContent = message;
  const close = document.createElement("button"); close.textContent = "×"; close.setAttribute("aria-label", "关闭提示"); close.onclick = () => card.remove();
  card.append(heading, body, close); $("toasts").append(card);
  while ($("toasts").children.length > 3) $("toasts").firstElementChild.remove();
  setTimeout(() => card.remove(), 6500);
}

async function api(route, payload) {
  const response = await fetch(route, payload === undefined ? {} : {
    method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

async function command(route, payload = {}) {
  try {
    requestVersion++; requestPending = true; updateControls();
    const data = await api(route, payload); render(data); return data;
  } catch (error) { toast("操作未完成", error.message); return null; }
  finally { requestPending = false; updateControls(); }
}

function drawDie(value) {
  const faces = {1:[4],2:[0,8],3:[0,4,8],4:[0,2,6,8],5:[0,2,4,6,8],6:[0,2,3,5,6,8]};
  const face = $("dice-face"); face.replaceChildren();
  face.className = value ? "dice-face" : "dice-face unknown";
  if (!value) { face.textContent = "?"; $("dice").setAttribute("aria-label", "点击骰子投点"); return; }
  for (let index = 0; index < 9; index++) {
    const pip = document.createElement("i"); pip.className = "pip";
    pip.style.visibility = faces[value].includes(index) ? "visible" : "hidden"; face.append(pip);
  }
  $("dice").setAttribute("aria-label", `骰子 ${value} 点`);
}

function drawBoard() {
  if (!current) return;
  const board = $("board"); board.style.setProperty("--cols", current.width);
  board.setAttribute("aria-rowcount", current.height); board.setAttribute("aria-colcount", current.width);
  board.replaceChildren();
  const treasures = new Set(current.treasures.map(pos => pos.join(",")));
  const spent = new Set(current.spent_traps.map(pos => pos.join(",")));
  const positions = displayPositions || {A:current.pos_a,B:current.pos_b};
  for (let row = 0; row < current.height; row++) for (let col = 0; col < current.width; col++) {
    const value = current.cells[row][col], key = `${row},${col}`;
    const treasure = treasures.has(key), wall = value === 1 && !treasure;
    const cell = document.createElement("button");
    cell.className = `cell${wall ? " wall" : treasure ? " treasure" : value < 0 ? " trap" : ""}${spent.has(key) ? " spent" : ""}`;
    cell.setAttribute("role", "gridcell");
    const text = wall ? "▧" : treasure ? `+${value}` : value < 0 ? String(value) : "";
    const label = wall ? "障碍物" : treasure ? `${value} 分宝藏` : value < 0 ? `${value} 分陷阱${spent.has(key) ? "（已失效）" : ""}` : "空地";
    cell.setAttribute("aria-label", `第 ${row + 1} 行第 ${col + 1} 列，${label}`);
    cell.disabled = current.running || requestPending || busy;
    const hereA = positions.A?.join(",") === key, hereB = positions.B?.join(",") === key;
    if (hereA || hereB) {
      const token = document.createElement("span"); token.className = `token${hereA && hereB ? " ab" : hereB ? " b" : ""}`;
      token.textContent = hereA && hereB ? "AB" : hereA ? "A" : "B"; cell.append(token);
      cell.setAttribute("aria-label", cell.getAttribute("aria-label") + `，玩家 ${token.textContent}`);
      if (text) { const under = document.createElement("span"); under.className = "under-token"; under.textContent = text; cell.append(under); }
    } else cell.textContent = text;
    cell.onclick = () => command("/api/map/edit", {row,col}); board.append(cell);
  }
}

function updateControls() {
  if (!current) return;
  const editing = !current.running && !requestPending && !busy;
  const input = current.phase === "input" && !requestPending && !busy;
  for (const id of ["generate","mode","start-human","start-machine","load","save"]) $(id).disabled = !editing;
  for (const field of $("map-form").elements) field.disabled = !editing;
  for (const id of ["action","submit","end-turn"]) $(id).disabled = !input;
  document.querySelectorAll("[data-direction]").forEach(button => button.disabled = !input);
  document.querySelectorAll("#board .cell").forEach(button => button.disabled = !editing);
  $("dice").disabled = current.phase !== "roll" || busy || requestPending;
  $("stop").disabled = !current.running || requestPending || current.phase === "stopping";
  $("report").disabled = !current.report_ready || requestPending || busy;
  $("map-help").textContent = current.running ? "对局进行中 · 地图编辑已暂停" : "点击格子编辑：空地 → 墙 → 宝藏 → 陷阱";
  $("input-hint").textContent = input ? "点击方向键或输入指令，按 Enter 执行。撞墙不消耗步数。" : current.phase === "roll" ? "请先点击上方骰子，投点后再移动。" : "轮到人类时，先点击骰子再移动。";
  updateSteps();
}

function updateSteps() {
  if (!current || current.dice == null || ["roll","rolling","idle"].includes(current.phase) || busy) { $("steps").textContent = "—"; return; }
  const tokens = $("action").value.trim().toLowerCase().split(/\s+/);
  let used = 0;
  if (current.phase === "input") for (const token of tokens) { if (token === "end") break; if (["w","a","s","d"].includes(token)) used++; }
  $("steps").textContent = `${Math.max(0,current.dice-used)} / ${current.dice}`;
  if (used > current.dice) $("input-hint").textContent = `最多 ${current.dice} 次有效移动，多余指令会被忽略。`;
}

function render(data) {
  if (current && data.frame_id < current.frame_id) return;
  current = data;
  $("connection").textContent = "游戏已连接";
  $("mode").value = data.mode; $("mode-note").textContent = notes[data.mode]; $("mode-pill").textContent = modes[data.mode];
  $("map-size").textContent = `${data.height} × ${data.width}`;
  $("score-a").textContent = data.score_a; $("score-b").textContent = data.score_b;
  $("label-a").textContent = data.labels.A; $("label-b").textContent = data.labels.B;
  $("remaining").textContent = `剩余宝藏 ${data.remaining} 枚`;
  $("phase").textContent = phaseNames[data.phase];
  $("round").textContent = data.round ? `第 ${data.round} 轮 / 玩家 ${data.turn}` : "准备开始";
  $("dice-title").textContent = data.phase === "roll" ? "点击骰子投点" : data.phase === "rolling" ? "骰子旋转中" : data.dice ? `${data.dice} 点` : "等待投骰";
  $("dice-hint").textContent = data.phase === "input" ? "轮到你了，选择探索方向。" : data.phase === "roll" ? "你的下一步，从这里开始。" : data.running ? `${data.turn}（${data.labels[data.turn]}）的回合` : "开始一场新的寻宝对决。";
  if (!data.running && !busy) { displayPositions = {A:data.pos_a,B:data.pos_b}; drawDie(data.dice); }
  if (data.phase === "roll" && !busy) drawDie(null);
  const logs = JSON.stringify(data.logs);
  if (logs !== lastLogs) {
    lastLogs = logs; $("logs").replaceChildren();
    for (const line of data.logs) { const p = document.createElement("p"); p.textContent = line; $("logs").append(p); }
    if (!data.logs.length) { const p = document.createElement("p"); p.className = "empty-log"; p.textContent = "每一步探索，都会在这里留下记录。"; $("logs").append(p); }
    $("logs").scrollTop = $("logs").scrollHeight;
  }
  updateControls(); drawBoard();
  if (!busy && data.frame_id !== lastFrame && data.round) runFrame(data);
  else if (!busy && data.phase === "rolling" && animatedDice !== data.frame_id) runDice(data);
  if (!busy && !requestPending && data.phase === "finished" && resultShown !== data.frame_id && data.result) {
    resultShown = data.frame_id;
    const result = data.result;
    $("result-title").textContent = result.winner ? `玩家 ${result.winner} 获胜` : "本局平局";
    $("result-text").textContent = `最终比分 A ${result.score_a} : ${result.score_b} B\n共 ${result.turns} 轮${result.timeout ? " · 达到回合上限" : " · 宝藏已全部拾取"}`;
    if ($("popups").checked && !$("result-dialog").open) $("result-dialog").showModal();
  }
}

async function runFrame(data) {
  busy = true; lastFrame = data.frame_id; updateControls();
  try {
    for (const event of data.events) {
      if (current.frame_id !== data.frame_id) break;
      if (event.kind === "MOVE" && event.actor && event.position) {
        if (!displayPositions) displayPositions = {A:data.pos_a,B:data.pos_b};
        displayPositions[event.actor] = event.position; drawBoard();
        if (animate()) await wait(80);
      }
      if ($("popups").checked && ["TRAP","BLOCKED","BATTLE"].includes(event.kind)) {
        toast(event.kind === "TRAP" ? "踩到陷阱！" : event.kind === "BLOCKED" ? "障碍物提示" : "对战结算", event.message);
      }
    }
    displayPositions = {A:data.pos_a,B:data.pos_b}; drawBoard();
  } finally { busy = false; }
  if (current.phase === "rolling" && current.frame_id === data.frame_id) await runDice(current);
  else render(current);
}

async function runDice(data) {
  busy = true; animatedDice = data.frame_id; updateControls();
  try {
    if (animate()) for (let tick = 0; tick < 10; tick++) {
      if (!current.running || current.frame_id !== data.frame_id || current.phase === "stopping") return;
      drawDie(1 + Math.floor(Math.random()*6)); $("dice").style.transform = `rotate(${tick*40}deg)`; await wait(75);
    }
    drawDie(data.dice); $("dice").style.transform = "rotate(0deg)";
    if (current.running && current.frame_id === data.frame_id && current.phase === "rolling") {
      const response = await api("/api/ready", {frame_id:data.frame_id}); render(response);
    }
  } catch (error) { toast("投骰未完成", error.message); }
  finally { busy = false; $("dice").style.transform = "rotate(0deg)"; render(current); }
}

$("map-form").onsubmit = event => {
  event.preventDefault(); const payload = {};
  for (const [key,value] of new FormData(event.target)) payload[key] = value === "" ? null : Number(value);
  command("/api/map/generate", payload);
};
$("mode").onchange = () => command("/api/mode", {mode:$("mode").value});
for (const kind of ["human","machine"]) $("start-"+kind).onclick = () => {
  $("action").value = ""; resultShown = null; command("/api/start", {kind});
};
$("stop").onclick = () => command("/api/stop");
$("dice").onclick = () => command("/api/roll", {frame_id:current.frame_id});
$("action-form").onsubmit = async event => {
  event.preventDefault(); const data = await command("/api/action", {frame_id:current.frame_id,action:$("action").value});
  if (data) $("action").value = "";
};
$("end-turn").onclick = async () => { const data = await command("/api/action", {frame_id:current.frame_id,action:"end"}); if (data) $("action").value = ""; };
$("action").oninput = updateSteps;
document.querySelectorAll("[data-direction]").forEach(button => button.onclick = () => {
  $("action").value = ($("action").value.trim()+" "+button.dataset.direction).trim(); $("action").focus(); updateSteps();
});
$("load").onclick = () => $("map-file").click();
$("map-file").onchange = async event => {
  const file = event.target.files[0]; if (file) await command("/api/map/load", {text:await file.text()}); event.target.value = "";
};
function download(route, name) {
  const link = document.createElement("a"); link.href = route; link.download = name;
  document.body.append(link); link.click(); link.remove();
}
$("save").onclick = () => download("/api/map/download", "dicehunt-map.txt");
$("report").onclick = () => download("/api/report", "dicehunt-report.txt");
$("result-close").onclick = () => $("result-dialog").close();
async function poll() {
  const version = requestVersion;
  try { const data = await api("/api/state"); if (version === requestVersion && !requestPending) render(data); }
  catch { $("connection").textContent = "连接已断开，请检查本地服务"; }
  finally { setTimeout(poll, 180); }
}
drawDie(null); poll();
