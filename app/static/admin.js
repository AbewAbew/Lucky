const tg = window.Telegram?.WebApp;
const $ = (id) => document.getElementById(id);
const state = { token: sessionStorage.getItem("lucky_token"), rooms: [], evidenceRounds: [], evidencePage: 1, evidenceTotal: 0, evidenceTotalPages: 1, evidenceSearch: "", deposits: [], withdrawals: [], transfers: [], realMoneyEnabled: false, roomId: null, game: null, evidence: null, evidenceRoom: null, soldCardNumbers: [], socket: null };

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let detail = "Request failed";
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}

function toast(message) {
  const element = $("admin-toast");
  element.textContent = message;
  element.classList.add("show");
  setTimeout(() => element.classList.remove("show"), 2500);
}

function money(value) { return (Number(value || 0) / 100).toFixed(2); }

const PROVIDER_LABELS = { telebirr: "Telebirr", cbe: "CBE Birr", cbe_account: "CBE Bank Account" };
function providerLabel(provider) {
  return PROVIDER_LABELS[provider] || String(provider || "").toUpperCase();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function ballClass(number) {
  return ["ball-b", "ball-i", "ball-n", "ball-g", "ball-o"][Math.floor((number - 1) / 15)] || "ball-b";
}

function countdown(room) {
  if (room.result_status === "pending") {
    const seconds = Math.max(0, Math.ceil((new Date(room.result_deadline_at).getTime() - Date.now()) / 1000));
    return `Calls stopped · result in ${seconds}s`;
  }
  if (room.result_status === "disputed") return "DISPUTED · payment frozen";
  if (room.state === "running") return "Live now";
  if (room.test_single_player_start) {
    const remaining = Math.max(0, 5 - Number(room.player_count || 0));
    if (remaining > 0) return `Test mode: ${remaining} more cartela${remaining === 1 ? "" : "s"}`;
    if (!room.auto_start_at) return "Test round starting soon";
    const seconds = Math.max(0, Math.ceil((new Date(room.auto_start_at).getTime() - Date.now()) / 1000));
    return `Test auto-start in ${seconds}s`;
  }
  const minimum = Number(room.auto_start_min_players || 5);
  const players = Number(room.unique_player_count || 0);
  if (players < minimum) {
    const remaining = minimum - players;
    return `Waiting for ${remaining} more player${remaining === 1 ? "" : "s"}`;
  }
  if (!room.auto_start_at) return "Starting soon";
  const seconds = Math.max(0, Math.ceil((new Date(room.auto_start_at).getTime() - Date.now()) / 1000));
  return `Auto-start in ${seconds}s`;
}

function refreshCountdowns() {
  document.querySelectorAll("[data-admin-countdown]").forEach((element) => {
    const room = state.rooms.find((item) => item.id === Number(element.dataset.adminCountdown));
    if (room) element.textContent = countdown(room);
  });
  if (state.game?.room) $("admin-countdown").textContent = countdown(state.game.room);
}

async function authenticate() {
  const config = await api("/api/config");
  const payload = { init_data: tg?.initData || "" };
  if (!payload.init_data && config.allow_dev_auth) {
    payload.dev_user_id = Number(localStorage.getItem("lucky_dev_id")) || 999000;
    payload.dev_first_name = "Demo Admin";
  }
  const auth = await api("/api/auth", { method: "POST", body: JSON.stringify(payload) });
  state.token = auth.token;
  sessionStorage.setItem("lucky_token", state.token);
}

async function loadEvidencePage(page = state.evidencePage) {
  const params = new URLSearchParams({
    page: String(page),
    page_size: "10",
    search: state.evidenceSearch,
  });
  const data = await api(`/api/control/evidence?${params}`);
  state.evidenceRounds = data.items;
  state.evidencePage = data.page;
  state.evidenceTotal = data.total;
  state.evidenceTotalPages = data.total_pages;
  renderEvidenceRounds();
  $("evidence-count").textContent = state.evidenceTotal;
}

async function loadDashboard() {
  const [data] = await Promise.all([
    api("/api/control/dashboard"),
    loadEvidencePage(),
  ]);
  state.rooms = data.rooms.filter((room) => (
    ["waiting", "running"].includes(room.state) && ["open", "pending"].includes(room.result_status)
  ) && room.stake_santim > 0);
  state.deposits = data.pending_deposits;
  state.withdrawals = data.pending_withdrawals;
  state.transfers = data.pending_transfers;
  state.realMoneyEnabled = data.real_money_enabled;
  if (state.roomId && !state.rooms.some((room) => room.id === state.roomId)) {
    state.socket?.close();
    state.socket = null;
    state.roomId = null;
    state.game = null;
    $("admin-live-board").classList.add("hidden");
  }
  renderRooms();
  renderDeposits();
  renderWithdrawals();
  renderTransfers();
  renderRevenue(data.revenue);
  $("deposit-count").textContent = state.deposits.length;
  $("withdrawal-count").textContent = state.withdrawals.length;
  $("transfer-count").textContent = state.transfers.length;
  $("withdrawal-warning").textContent = state.realMoneyEnabled
    ? "Send the player’s money first. Then enter the bank payout transaction reference and approve. Rejecting releases the reserved balance."
    : "TEST MODE — DO NOT SEND MONEY. You may inspect or reject requests, but approval is locked until real-money mode is enabled.";
}

function renderRevenue(revenue) {
  if (!revenue) return;
  const rounds = (count) => `${count} round${count === 1 ? "" : "s"} settled`;
  $("revenue-today").textContent = `${money(revenue.today.commission_santim)} birr`;
  $("revenue-today-rounds").textContent = rounds(revenue.today.settled_rounds);
  $("revenue-week").textContent = `${money(revenue.last_7_days.commission_santim)} birr`;
  $("revenue-week-rounds").textContent = rounds(revenue.last_7_days.settled_rounds);
  $("revenue-month").textContent = `${money(revenue.last_30_days.commission_santim)} birr`;
  $("revenue-month-rounds").textContent = rounds(revenue.last_30_days.settled_rounds);
  $("revenue-all-time").textContent = `${money(revenue.all_time.commission_santim)} birr`;
  $("revenue-all-time-rounds").textContent = rounds(revenue.all_time.settled_rounds);
  $("revenue-gross-pool").textContent = `${money(revenue.all_time.gross_pool_santim)} birr`;
  $("revenue-transfer-cost").textContent = `${money(revenue.all_time.transfer_cost_santim)} birr`;
  $("revenue-payouts").textContent = `${money(revenue.all_time.payout_santim)} birr`;
  $("revenue-dismissed").textContent = revenue.dismissed_rounds;
  $("revenue-by-tier").innerHTML = revenue.by_tier.length
    ? revenue.by_tier.map((tier) => `
      <div class="revenue-tier-row">
        <span>${money(tier.stake_santim)} birr tier</span>
        <span>${rounds(tier.settled_rounds)}</span>
        <strong>${money(tier.commission_santim)} birr</strong>
      </div>`).join("")
    : `<div class="empty-state">No settled rounds yet.</div>`;
}

function renderRooms() {
  $("admin-room-list").innerHTML = state.rooms.map((room) => `
    <button class="admin-room ${room.id === state.roomId ? "selected" : ""}" data-room="${room.id}">
      <h3>${escapeHtml(room.name)} <small>Round #${room.id}</small></h3><p>${room.player_count}/400 cards · ${room.unique_player_count}/${room.test_single_player_start ? 1 : 5} players · ${room.result_status === "open" ? room.state : room.result_status}<br><b data-admin-countdown="${room.id}">${countdown(room)}</b><br>Winner: ${money(room.winner_payout_santim)} birr</p>
    </button>`).join("");
  document.querySelectorAll(".admin-room").forEach((button) => {
    button.addEventListener("click", () => selectRoom(Number(button.dataset.room)));
  });
}

async function selectRoom(roomId) {
  state.roomId = roomId;
  const [game, sold] = await Promise.all([
    api(`/api/rooms/${roomId}/game`),
    api(`/api/control/rooms/${roomId}/sold-cards`),
  ]);
  state.game = game;
  state.soldCardNumbers = sold.sold_card_numbers;
  renderRooms();
  renderBoard();
  $("admin-live-board").classList.remove("hidden");
  connectSocket();
}

function renderBoard() {
  const { room, draws } = state.game;
  const called = new Set(draws);
  $("admin-room-name").textContent = room.name;
  const visibleStatus = room.result_status === "open" ? room.state : room.result_status;
  $("admin-game-status").textContent = visibleStatus.toUpperCase();
  $("admin-game-status").className = `status-dot ${room.result_status === "open" ? room.state : room.result_status}`;
  const latest = draws.at(-1);
  const latestLetter = latest ? "BINGO"[Math.floor((latest - 1) / 15)] : "";
  $("admin-latest-ball").innerHTML = latest ? `<small>${latestLetter}</small><strong>${latest}</strong>` : "—";
  $("admin-latest-ball").className = `latest-ball ${latest ? ballClass(latest) : ""}`;
  $("admin-finance").innerHTML = `
    <div><span>CARDS SOLD</span><strong>${room.player_count}/400</strong></div>
    <div><span>UNIQUE PLAYERS</span><strong>${room.unique_player_count}/${room.test_single_player_start ? 1 : 5}</strong></div>
    <div><span>TOTAL BET</span><strong>${money(room.gross_pool_santim)} birr</strong></div>
    <div><span>5% COMMISSION</span><strong>${money(room.commission_santim)} birr</strong></div>
    <div><span>${room.winner_count > 1 ? `EACH OF ${room.winner_count} WINNERS` : "WINNER PAYOUT"}</span><strong>${money(room.winner_count > 1 ? Math.floor(room.winner_payout_santim / room.winner_count) : room.winner_payout_santim)} birr</strong></div>`;
  $("number-board").innerHTML = Array.from({ length: 75 }, (_, index) => index + 1)
    .map((number) => `<span class="board-number ${called.has(number) ? "called" : ""}">${number}</span>`).join("");
  const soldCards = new Set(state.soldCardNumbers);
  $("sold-card-board").innerHTML = Array.from({ length: 400 }, (_, index) => index + 1)
    .map((number) => `<span class="sold-card-number ${soldCards.has(number) ? "sold" : ""}">${number}</span>`).join("");
  $("sold-card-count").textContent = `${soldCards.size}/400 marked`;
  $("admin-countdown").textContent = countdown(room);
  const minimum = Number(room.auto_start_min_players || 5);
  const players = Number(room.unique_player_count || 0);
  const ready = room.test_single_player_start
    ? players >= 1 && Number(room.player_count || 0) >= 5
    : players >= minimum;
  $("admin-start-game").disabled = room.state !== "waiting" || !ready;
  $("admin-start-game").textContent = room.result_status === "pending"
    ? "Result review in progress"
    : room.result_status === "disputed"
      ? "DISPUTED · payment frozen"
      : room.state === "waiting"
    ? !ready
      ? room.test_single_player_start
        ? `Need ${Math.max(0, 5 - Number(room.player_count || 0))} more test cartelas`
        : `Need ${minimum - players} more player${minimum - players === 1 ? "" : "s"}`
      : "Start now"
    : "Game in progress";
}

function renderEvidence() {
  const panel = $("evidence-detail");
  const evidence = state.evidence;
  if (!evidence) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
    return;
  }
  const room = state.evidenceRoom;
  const winnerRows = evidence.winners.map((winner) => `
    <li><strong>Cartela #${winner.card_number}</strong><span>${escapeHtml(winner.first_name)} · Telegram ${winner.telegram_id}</span></li>
  `).join("");
  const disputeRows = evidence.disputes.map((dispute) => `
    <li><strong>${escapeHtml(dispute.first_name)} · ${new Date(dispute.created_at).toLocaleString()}</strong><span>${escapeHtml(dispute.reason)}</span></li>
  `).join("");
  panel.classList.toggle("disputed", room.result_status === "disputed");
  panel.innerHTML = `
    <div class="evidence-head"><div><span>ROUND EVIDENCE</span><h3>Game #${room.id} · ${room.result_status.toUpperCase()}</h3></div><strong>Final call ${evidence.final_called_number}</strong></div>
    <div class="evidence-meta">Started ${evidence.room_started_at ? new Date(evidence.room_started_at).toLocaleString() : "—"} · Detected ${new Date(evidence.created_at).toLocaleString()} · Call #${evidence.winning_sequence} · ${evidence.players.length} cartela record(s)</div>
    <h4>Winning cartelas</h4><ul>${winnerRows}</ul>
    ${disputeRows ? `<h4>Player dispute</h4><ul class="dispute-list">${disputeRows}</ul>` : ""}
    <details><summary>Called-number timeline (${evidence.draws.length})</summary><div class="evidence-timeline">${evidence.draws.map((draw) => `<span>#${draw.sequence} · ${draw.number}<small>${new Date(draw.called_at).toLocaleString()}</small></span>`).join("")}</div></details>
    <details><summary>Player and cartela records (${evidence.players.length})</summary><div class="evidence-players">${evidence.players.map((player) => `<span>Cartela #${player.card_number} · ${escapeHtml(player.first_name)} · Telegram ${player.telegram_id}<small>${new Date(player.card_created_at).toLocaleString()}</small></span>`).join("")}</div></details>`;
  panel.classList.remove("hidden");
}

function renderEvidenceRounds() {
  const rounds = state.evidenceRounds;
  $("evidence-round-list").innerHTML = rounds.length ? rounds.map((round) => `
    <button class="evidence-round-card ${round.result_status === "disputed" ? "disputed" : ""}" data-evidence-room="${round.id}">
      <div><span>GAME ID</span><strong>Round #${round.id}</strong><small>${escapeHtml(round.name)} · ${money(round.stake_santim)} birr</small></div>
      <div><span>RESULT</span><strong>${String(round.result_status || round.outcome || "recorded").toUpperCase()}</strong><small>${round.winner_count} winner${round.winner_count === 1 ? "" : "s"} · final call ${round.final_called_number}</small></div>
      <time>${new Date(round.evidence_created_at).toLocaleString()}</time>
    </button>
  `).join("") : `<div class="empty-state">No round evidence matches this Game ID.</div>`;
  document.querySelectorAll("[data-evidence-room]").forEach((button) => {
    button.addEventListener("click", () => selectEvidenceRound(Number(button.dataset.evidenceRoom)));
  });
  $("evidence-page-status").textContent = `Page ${state.evidencePage} of ${state.evidenceTotalPages} · ${state.evidenceTotal} game${state.evidenceTotal === 1 ? "" : "s"}`;
  $("evidence-prev").disabled = state.evidencePage <= 1;
  $("evidence-next").disabled = state.evidencePage >= state.evidenceTotalPages;
}

async function selectEvidenceRound(roomId) {
  try {
    const [evidence, game] = await Promise.all([
      api(`/api/control/rooms/${roomId}/evidence`),
      api(`/api/rooms/${roomId}/game`),
    ]);
    state.evidence = evidence;
    state.evidenceRoom = game.room;
    renderEvidence();
    $("evidence-detail").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) { toast(error.message); }
}

function connectSocket() {
  state.socket?.close();
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  state.socket = new WebSocket(`${protocol}://${location.host}/ws/rooms/${state.roomId}?token=${encodeURIComponent(state.token)}`);
  state.socket.addEventListener("message", async ({ data }) => {
    const event = JSON.parse(data);
    if (event.type === "connected") state.game = event.state;
    if (event.room) {
      state.game.room = event.room;
      state.rooms = state.rooms.map((room) => room.id === event.room.id ? event.room : room);
    }
    if (event.sold_card_numbers) state.soldCardNumbers = event.sold_card_numbers;
    if (event.draws) state.game.draws = event.draws;
    if (event.winners) state.game.winners = event.winners;
    if (event.type === "bingo_pending") toast(`${event.winners.length} winning card(s); calls stopped for 6-second review`);
    if (event.type === "game_disputed") toast(`Round #${event.room.id} disputed; payment frozen`);
    if (event.type === "game_settled") toast(`Payout split between ${event.winners.length} winner(s)`);
    if (event.type === "game_dismissed") toast("More than four winners: round dismissed and refunded");
    renderBoard();
    renderRooms();
    if (["bingo_pending", "game_disputed", "game_settled", "game_dismissed"].includes(event.type)) {
      state.evidencePage = 1;
      const finishedStake = Number(event.room?.stake_santim || 0);
      await loadDashboard();
      if (["game_disputed", "game_settled", "game_dismissed"].includes(event.type)) {
        const replacement = state.rooms.find((room) => room.stake_santim === finishedStake && room.state === "waiting");
        if (replacement) await selectRoom(replacement.id);
      }
    }
  });
}

async function startGame() {
  try {
    state.game.room = await api(`/api/control/rooms/${state.roomId}/start`, { method: "POST", body: "{}" });
    renderBoard();
    toast("Lucky game started");
  } catch (error) { toast(error.message); }
}

function renderDeposits() {
  $("deposit-list").innerHTML = state.deposits.length ? state.deposits.map((deposit) => `
    <article class="deposit-card">
      <div class="deposit-card-head"><h3>#${deposit.id} · ${escapeHtml(deposit.first_name)}</h3><strong>${money(deposit.amount_santim)} birr</strong></div>
      <div class="deposit-meta">${providerLabel(deposit.provider)} · Telegram ${deposit.telegram_id}<br>Transaction ID: <b>${escapeHtml(deposit.transaction_id)}</b><br>Submitted: ${new Date(deposit.submitted_at).toLocaleString()}</div>
      <div class="deposit-actions"><button class="approve" data-review="${deposit.id}" data-approve="true">Approve</button><button class="reject" data-review="${deposit.id}" data-approve="false">Reject</button></div>
    </article>`).join("") : `<div class="empty-state">No pending deposits.</div>`;
  document.querySelectorAll("[data-review]").forEach((button) => {
    button.addEventListener("click", () => reviewDeposit(Number(button.dataset.review), button.dataset.approve === "true"));
  });
}

async function reviewDeposit(depositId, approve) {
  try {
    await api(`/api/control/deposits/${depositId}/review`, { method: "POST", body: JSON.stringify({ approve }) });
    toast(approve ? "Deposit approved and wallet credited" : "Deposit rejected");
    await loadDashboard();
  } catch (error) { toast(error.message); }
}

function renderWithdrawals() {
  $("withdrawal-list").innerHTML = state.withdrawals.length ? state.withdrawals.map((withdrawal) => `
    <article class="deposit-card withdrawal-card">
      <div class="deposit-card-head"><h3>#${withdrawal.id} · ${escapeHtml(withdrawal.first_name)}</h3><strong>${money(withdrawal.amount_santim)} birr</strong></div>
      <div class="deposit-meta">${providerLabel(withdrawal.provider)} · Telegram ${withdrawal.telegram_id}<br>Account: <b>${escapeHtml(withdrawal.account_number)}</b><br>Account name: <b>${escapeHtml(withdrawal.account_name)}</b><br>Submitted: ${new Date(withdrawal.submitted_at).toLocaleString()}</div>
      <input id="withdrawal-reference-${withdrawal.id}" class="withdrawal-reference" maxlength="120" placeholder="Bank payout transaction reference" />
      <div class="deposit-actions"><button class="approve" data-withdrawal-review="${withdrawal.id}" data-approve="true" ${state.realMoneyEnabled ? "" : "disabled"}>${state.realMoneyEnabled ? "Paid · Approve" : "Test mode"}</button><button class="reject" data-withdrawal-review="${withdrawal.id}" data-approve="false">Reject</button></div>
    </article>`).join("") : `<div class="empty-state">No pending withdrawals.</div>`;
  document.querySelectorAll("[data-withdrawal-review]").forEach((button) => {
    button.addEventListener("click", () => reviewWithdrawal(Number(button.dataset.withdrawalReview), button.dataset.approve === "true"));
  });
}

async function reviewWithdrawal(withdrawalId, approve) {
  const payoutReference = $(`withdrawal-reference-${withdrawalId}`).value.trim();
  if (approve && !payoutReference) return toast("Enter the bank payout transaction reference first");
  try {
    await api(`/api/control/withdrawals/${withdrawalId}/review`, {
      method: "POST",
      body: JSON.stringify({ approve, payout_reference: approve ? payoutReference : null }),
    });
    toast(approve ? "Withdrawal approved and reserved balance paid" : "Withdrawal rejected and balance released");
    await loadDashboard();
  } catch (error) { toast(error.message); }
}

function renderTransfers() {
  $("transfer-list").innerHTML = state.transfers.length ? state.transfers.map((transfer) => `
    <article class="deposit-card withdrawal-card">
      <div class="deposit-card-head"><h3>#${transfer.id} · ${escapeHtml(transfer.sender_first_name)} → ${escapeHtml(transfer.recipient_first_name)}</h3><strong>${money(transfer.amount_santim)} birr</strong></div>
      <div class="deposit-meta">From Telegram ${transfer.sender_telegram_id} · To Telegram ${transfer.recipient_telegram_id}<br>Submitted: ${new Date(transfer.submitted_at).toLocaleString()}</div>
      <div class="transfer-balance-check">
        <span>Sender before<strong>${money(transfer.sender_balance_before_santim)} birr</strong></span>
        <span>Sender after<strong class="withdrawable">${money(transfer.sender_balance_after_santim)} birr</strong></span>
        <span>Recipient before<strong>${money(transfer.recipient_balance_before_santim)} birr</strong></span>
        <span>Recipient after<strong class="withdrawable">${money(transfer.recipient_balance_after_santim)} birr</strong></span>
      </div>
      ${transfer.sender_bonus_santim ? `<p class="transfer-bonus-note">Sender's balance includes ${money(transfer.sender_bonus_santim)} birr locked bonus, already excluded from what they can send.</p>` : ""}
      <div class="deposit-actions"><button class="approve" data-transfer-review="${transfer.id}" data-approve="true">Approve</button><button class="reject" data-transfer-review="${transfer.id}" data-approve="false">Reject</button></div>
    </article>`).join("") : `<div class="empty-state">No pending transfers.</div>`;
  document.querySelectorAll("[data-transfer-review]").forEach((button) => {
    button.addEventListener("click", () => reviewTransfer(Number(button.dataset.transferReview), button.dataset.approve === "true"));
  });
}

async function reviewTransfer(transferId, approve) {
  try {
    await api(`/api/control/transfers/${transferId}/review`, { method: "POST", body: JSON.stringify({ approve }) });
    toast(approve ? "Transfer approved and balance moved" : "Transfer rejected and balance released");
    await loadDashboard();
  } catch (error) { toast(error.message); }
}

document.querySelectorAll(".admin-tab").forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll(".admin-tab").forEach((item) => item.classList.toggle("active", item === tab));
  $("games-tab").classList.toggle("hidden", tab.dataset.tab !== "games");
  $("evidence-tab").classList.toggle("hidden", tab.dataset.tab !== "evidence");
  $("deposits-tab").classList.toggle("hidden", tab.dataset.tab !== "deposits");
  $("withdrawals-tab").classList.toggle("hidden", tab.dataset.tab !== "withdrawals");
  $("transfers-tab").classList.toggle("hidden", tab.dataset.tab !== "transfers");
  $("revenue-tab").classList.toggle("hidden", tab.dataset.tab !== "revenue");
}));
$("admin-refresh").addEventListener("click", () => loadDashboard().catch((error) => toast(error.message)));
$("admin-start-game").addEventListener("click", startGame);
let evidenceSearchTimer;
$("evidence-search").addEventListener("input", (event) => {
  clearTimeout(evidenceSearchTimer);
  state.evidenceSearch = event.target.value.trim().replace(/^#/, "");
  state.evidencePage = 1;
  evidenceSearchTimer = setTimeout(() => loadEvidencePage(1).catch((error) => toast(error.message)), 250);
});
$("evidence-prev").addEventListener("click", () => loadEvidencePage(state.evidencePage - 1).catch((error) => toast(error.message)));
$("evidence-next").addEventListener("click", () => loadEvidencePage(state.evidencePage + 1).catch((error) => toast(error.message)));
setInterval(refreshCountdowns, 1000);
setInterval(() => loadDashboard().catch(() => {}), 5000);

async function boot() {
  try {
    tg?.ready(); tg?.expand();
    await authenticate();
    await loadDashboard();
    $("admin-loading").classList.add("hidden");
    $("admin-dashboard").classList.remove("hidden");
  } catch (error) {
    $("admin-loading").classList.add("hidden");
    $("admin-error-copy").textContent = error.message;
    $("admin-error").classList.remove("hidden");
  }
}

boot();
