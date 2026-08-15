import { LOCALES } from "./locales.js";

const tg = window.Telegram?.WebApp;

// ---- i18n engine ----
// Translation data lives entirely in locales.js. Everything below is just
// lookup/interpolation/persistence — never add a hardcoded English or
// Amharic string here; add a key to locales.js instead and call t()/tn().
function detectDefaultLocale() {
  const saved = localStorage.getItem("lucky_locale");
  if (saved && LOCALES[saved]) return saved;
  const telegramLang = (tg?.initDataUnsafe?.user?.language_code || "").toLowerCase();
  // Telegram reports language_code as an ISO 639-1 prefix (e.g. "am", "ti").
  const match = Object.keys(LOCALES).find((locale) => locale !== "en" && telegramLang.startsWith(locale));
  return match || "en";
}

let currentLocale = detectDefaultLocale();

function t(key, vars = {}) {
  let template = LOCALES[currentLocale]?.[key] ?? LOCALES.en[key] ?? key;
  for (const [name, value] of Object.entries(vars)) {
    template = template.replaceAll(`{${name}}`, String(value));
  }
  return template;
}

// Picks the "_one" or "_other" variant of a key based on count, and makes
// {n} available to interpolate inside that variant automatically.
function tn(key, count, vars = {}) {
  return t(`${key}_${count === 1 ? "one" : "other"}`, { n: count, ...vars });
}

function birr(santim) {
  return `${money(santim)} ${t("unit_birr")}`;
}

function applyStaticTranslations() {
  document.querySelectorAll("[data-i18n]").forEach((element) => {
    element.textContent = t(element.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-html]").forEach((element) => {
    element.innerHTML = t(element.dataset.i18nHtml);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((element) => {
    element.placeholder = t(element.dataset.i18nPlaceholder);
  });
  document.querySelectorAll("[data-i18n-aria-label]").forEach((element) => {
    element.setAttribute("aria-label", t(element.dataset.i18nAriaLabel));
  });
  document.documentElement.lang = currentLocale;
  document.querySelectorAll("[data-locale-option]").forEach((button) => {
    button.classList.toggle("active", button.dataset.localeOption === currentLocale);
  });
}

function setLocale(locale) {
  if (!LOCALES[locale] || locale === currentLocale) return;
  currentLocale = locale;
  localStorage.setItem("lucky_locale", locale);
  applyStaticTranslations();
  // Re-render whatever dynamic screen is currently visible so its
  // JS-built strings (toasts aside) pick up the new language immediately.
  if (state.game && !$("game-screen").classList.contains("hidden")) renderGame();
  else if (!$("lobby-screen").classList.contains("hidden")) renderLobby();
}

const state = {
  config: null,
  token: sessionStorage.getItem("lucky_token"),
  user: null,
  rooms: [],
  leaders: [],
  wallet: null,
  game: null,
  roomId: null,
  pendingRoomId: null,
  availableCards: [],
  ownedCards: [],
  pendingSelections: [],
  previewCard: null,
  previewCardNumber: null,
  maximumCards: 5,
  pendingWalletTab: null,
  socket: null,
  pingTimer: null,
  announcedResultKey: null,
  watchedCards: {},
  walletHistoryActivity: [],
  walletHistoryPage: 1,
};

const $ = (id) => document.getElementById(id);
const screens = ["loading-screen", "error-screen", "lobby-screen", "game-screen"];

function showScreen(id) {
  screens.forEach((screen) => $(screen).classList.toggle("hidden", screen !== id));
}

function toast(message) {
  const element = $("toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(element.timer);
  element.timer = setTimeout(() => element.classList.remove("show"), 2600);
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let detail = t("something_went_wrong");
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function parseStartParameter() {
  const raw = tg?.initDataUnsafe?.start_param || new URLSearchParams(location.search).get("startapp") || "";
  const room = raw.match(/^room_(\d+)$/);
  const referral = raw.match(/^ref_(\d+)$/);
  const walletTab = ["deposit", "withdraw", "transfer", "history"].includes(raw) ? raw : null;
  return { roomId: room ? Number(room[1]) : null, referralId: referral ? Number(referral[1]) : null, walletTab };
}

function watchedCardsStorageKey() {
  return `lucky_watched_cards_${state.user?.telegram_id}`;
}

function loadWatchedCards() {
  try {
    state.watchedCards = JSON.parse(localStorage.getItem(watchedCardsStorageKey())) || {};
  } catch (_) {
    state.watchedCards = {};
  }
}

function saveWatchedCards() {
  try {
    localStorage.setItem(watchedCardsStorageKey(), JSON.stringify(state.watchedCards));
  } catch (_) { /* storage unavailable or full — watch list just won't persist */ }
}

function pruneWatchedCards() {
  const activeRoomIds = new Set(state.rooms.map((room) => String(room.id)));
  let changed = false;
  for (const roomId of Object.keys(state.watchedCards)) {
    if (!activeRoomIds.has(roomId)) {
      delete state.watchedCards[roomId];
      changed = true;
    }
  }
  if (changed) saveWatchedCards();
}

async function authenticate() {
  state.config = await api("/api/config");
  const start = parseStartParameter();
  const body = { init_data: tg?.initData || "", referral_telegram_id: start.referralId };
  if (!body.init_data && state.config.allow_dev_auth) {
    body.dev_user_id = Number(localStorage.getItem("lucky_dev_id")) || 999000;
    body.dev_first_name = localStorage.getItem("lucky_dev_name") || "Demo Player";
  }
  const auth = await api("/api/auth", { method: "POST", body: JSON.stringify(body) });
  state.token = auth.token;
  state.user = auth.user;
  state.signupBonusSantim = Number(auth.signup_bonus_santim || 0);
  state.pendingWalletTab = start.walletTab;
  sessionStorage.setItem("lucky_token", state.token);
  loadWatchedCards();
  return start.roomId;
}

async function loadLobby() {
  disconnectSocket();
  const [rooms, wallet] = await Promise.all([api("/api/rooms"), api("/api/wallet")]);
  state.rooms = [200, 500, 1000]
    .map((stake) => rooms.find((room) => room.stake_santim === stake && ["waiting", "running"].includes(room.state)))
    .filter(Boolean);
  pruneWatchedCards();
  state.wallet = wallet;
  renderLobby();
  showScreen("lobby-screen");
}

function money(santim) {
  return `${(Number(santim || 0) / 100).toFixed(2)}`;
}

const PROVIDER_LABEL_KEYS = { telebirr: "provider_telebirr", cbe: "provider_cbe", cbe_account: "provider_cbe_account" };
function providerLabel(provider) {
  const key = PROVIDER_LABEL_KEYS[provider];
  return key ? t(key) : String(provider || "").toUpperCase();
}

function setAvailableBalance(balanceSantim) {
  const balance = Number(balanceSantim || 0);
  if (state.wallet) state.wallet.balance_santim = balance;
  if (state.game) state.game.balance_santim = balance;
  if ($("wallet-balance")) $("wallet-balance").textContent = birr(balance);
  if ($("game-wallet-balance")) $("game-wallet-balance").textContent = birr(balance);
}

async function refreshAvailableBalance() {
  try {
    const wallet = await api("/api/wallet");
    state.wallet = wallet;
    setAvailableBalance(wallet.balance_santim);
  } catch (_) {}
}

function winningsFor(telegramId, winners = []) {
  const cards = winners.filter((winner) => winner.telegram_id === telegramId);
  return {
    cards,
    payoutSantim: cards.reduce((total, winner) => total + Number(winner.payout_santim || 0), 0),
  };
}

function lobbyCountdown(room) {
  if (room.result_status === "pending") return t("status_result_review");
  if (room.result_status === "disputed") return t("status_result_disputed");
  if (room.state === "running") return t("status_live_now");
  if (room.test_single_player_start) {
    const remaining = Math.max(0, 5 - Number(room.player_count || 0));
    if (remaining > 0) return tn("status_test_cartelas_needed", remaining);
    if (!room.auto_start_at) return t("status_test_starting_soon");
    const seconds = Math.max(0, Math.ceil((new Date(room.auto_start_at).getTime() - Date.now()) / 1000));
    return t("status_test_starts_in", { seconds });
  }
  const minimum = Number(room.auto_start_min_players || 5);
  const players = Number(room.unique_player_count || 0);
  if (players < minimum) {
    const remaining = minimum - players;
    return tn("status_players_needed", remaining);
  }
  if (!room.auto_start_at) return t("status_starting_soon");
  const seconds = Math.max(0, Math.ceil((new Date(room.auto_start_at).getTime() - Date.now()) / 1000));
  return t("status_starts_in", { seconds });
}

function ballClass(number) {
  if (!number) return "";
  const index = Math.floor((number - 1) / 15);
  return ["ball-b", "ball-i", "ball-n", "ball-g", "ball-o"][index] || "ball-b";
}

function refreshCountdowns() {
  document.querySelectorAll("[data-room-countdown]").forEach((element) => {
    const room = state.rooms.find((item) => item.id === Number(element.dataset.roomCountdown));
    if (room) element.textContent = lobbyCountdown(room);
  });
  if (state.game?.room) {
    const room = state.game.room;
    const draws = state.game.draws || [];
    const resultPending = room.result_status === "pending";
    const resultDisputed = room.result_status === "disputed";
    const startsAt = room.auto_start_at ? new Date(room.auto_start_at).getTime() : null;
    const seconds = startsAt ? Math.max(0, Math.ceil((startsAt - Date.now()) / 1000)) : null;
    const resultSeconds = room.result_deadline_at
      ? Math.max(0, Math.ceil((new Date(room.result_deadline_at).getTime() - Date.now()) / 1000))
      : 0;
    if ($("timer-label")) {
      $("timer-label").textContent = resultPending
        ? t("result_confirms_in")
        : resultDisputed ? t("result_status_label") : room.state === "waiting" ? t("game_starts_in_label") : t("numbers_called_label");
    }
    if ($("call-timer-text")) {
      $("call-timer-text").textContent = resultPending
        ? `${resultSeconds}s`
        : resultDisputed ? t("hold") : room.state === "waiting"
        ? seconds === null ? "—" : `${seconds}s`
        : room.state === "running" ? `${draws.length}/75` : t("done");
    }
    if ($("timer-progress")) {
      const pct = resultPending
        ? Math.max(0, Math.min(100, (resultSeconds / Number(state.config?.result_confirmation_seconds || 6)) * 100))
        : room.state === "running"
        ? Math.max(0, Math.min(100, (draws.length / 75) * 100))
        : seconds === null ? 0 : Math.max(0, Math.min(100,
          (seconds / Number(state.config?.auto_start_delay_seconds || 1)) * 100));
      $("timer-progress").style.strokeDasharray = `${pct}, 100`;
    }
    if ($("call-message")) {
      $("call-message").textContent = resultPending
        ? t("bingo_detected_calls_stopped")
        : resultDisputed ? t("payment_frozen_disputed") : room.state === "waiting"
        ? lobbyCountdown(room)
        : t("numbers_called_of_75", { count: state.game.draws?.length || 0 });
    }
    if ($("result-review-countdown") && resultPending) {
      $("result-review-countdown").textContent = `${resultSeconds}s`;
    }
  }
}

function renderLobby() {
  $("welcome-name").textContent = state.user?.first_name || t("welcome_default_name");
  $("profile-button").textContent = (state.user?.first_name || "P").slice(0, 1).toUpperCase();
  $("wallet-balance").textContent = birr(state.wallet?.balance_santim);
  $("test-mode-banner").classList.toggle("hidden", !state.config?.test_single_player_start);
  const bonusSantim = Number(state.config?.signup_bonus_santim || 0);
  $("bonus-banner").classList.toggle("hidden", bonusSantim <= 0);
  if (bonusSantim > 0) {
    $("bonus-banner-copy").textContent = t("free_bonus_copy", { amount: money(bonusSantim) });
  }

  $("room-list").innerHTML = state.rooms.map((room) => {
    const running = room.state === "running";
    const playerCount = Number(room.unique_player_count || 0);
    const cardCount = Number(room.player_count || 0);
    return `<article class="room-card">
      <div class="room-symbol">${money(room.stake_santim).replace(".00", "")}</div>
      <div class="room-copy">
        <h3>${escapeHtml(room.name)}</h3>
        <div class="room-meta">
          <span class="meta-item"><svg class="meta-icon" width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/></svg>${tn("room_players", playerCount)}</span>
          <span class="meta-dot">•</span>
          <span class="meta-item"><svg class="meta-icon" width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M20 4H4c-1.11 0-1.99.89-1.99 2L2 18c0 1.11.89 2 2 2h16c1.11 0 2-.89 2-2V6c0-1.11-.89-2-2-2zm0 14H4V6h16v12z"/></svg>${tn("room_cartelas", cardCount)}</span>
        </div>
        <small class="room-countdown" data-room-countdown="${room.id}">${lobbyCountdown(room)}</small>
      </div>
      <button class="join-button" data-room="${room.id}">${running ? t("room_watch_live") : t("room_choose")}</button>
      <div class="room-finance">
        <span>${t("room_stake_per_cartela")}<strong>${birr(room.stake_santim)}</strong></span>
        <span class="net">${t("room_winner_receives")}<strong>${birr(room.winner_payout_santim)}</strong></span>
      </div>
    </article>`;
  }).join("") || `<div class="empty-state">${t("room_list_empty")}</div>`;

  document.querySelectorAll("[data-room]").forEach((button) => {
    button.addEventListener("click", async () => {
      const roomId = Number(button.dataset.room);
      const room = state.rooms.find((item) => item.id === roomId);
      const hasWatchedCards = (state.watchedCards[roomId] || []).length > 0;
      try {
        if (room?.state === "running" || hasWatchedCards) await enterGame(roomId, null);
        else await openCardChooser(roomId);
      } catch (error) {
        toast(error.message);
      }
    });
  });
}

async function openCardChooser(roomId) {
  const room = state.rooms.find((item) => item.id === Number(roomId)) || state.game?.room;
  if (!room) throw new Error(t("error_game_room_not_found"));
  if (room.state === "running") {
    toast(t("toast_game_already_started"));
    return enterGame(room.id, null);
  }

  const selection = await api(`/api/rooms/${room.id}/available-cards`);
  state.pendingRoomId = room.id;
  state.availableCards = selection.available || [];
  state.ownedCards = selection.owned || [];
  state.maximumCards = selection.maximum || 5;
  state.pendingSelections = [];
  state.previewCard = null;
  state.previewCardNumber = null;

  $("choice-room").textContent = t("choice_room_available", { name: room.name, count: room.available_cards ?? state.availableCards.length });
  $("cartela-preview").classList.add("hidden");
  renderCardPicker();
  $("card-modal").classList.remove("hidden");
}

function renderCardPicker() {
  const room = state.rooms.find((item) => item.id === state.pendingRoomId) || state.game?.room;
  const capacity = Number(room?.card_capacity || 400);
  const available = new Set(state.availableCards);
  const owned = new Set(state.ownedCards);
  const selected = new Set(state.pendingSelections);
  const remainingSlots = Math.max(0, state.maximumCards - state.ownedCards.length - state.pendingSelections.length);

  $("choice-count").textContent = state.ownedCards.length
    ? t("choice_count_own_more", { owned: state.ownedCards.length, remaining: remainingSlots, max: state.maximumCards })
    : t("choice_count_choose_up_to", { max: state.maximumCards });
  $("selected-cartelas").innerHTML = [
    ...state.ownedCards.map((number) => `<span class="selected-cartela owned">${t("selected_owned", { number })}</span>`),
    ...state.pendingSelections.map((number) => `<button class="selected-cartela" data-remove-cartela="${number}">${t("selected_remove", { number })}</button>`),
  ].join("") || `<span class="selection-placeholder">${t("selection_placeholder")}</span>`;

  $("card-number-grid").innerHTML = Array.from({ length: capacity }, (_, index) => {
    const number = index + 1;
    const isOwned = owned.has(number);
    const isSelected = selected.has(number);
    const isPreviewed = state.previewCardNumber === number;
    const isTaken = !available.has(number) && !isOwned;
    const ariaSuffix = isOwned ? t("cartela_owned_aria") : isSelected ? t("cartela_selected_aria") : isTaken ? t("cartela_taken_aria") : "";
    return `<button class="cartela-number ${isOwned ? "owned" : ""} ${isSelected ? "selected" : ""} ${isPreviewed ? "previewing" : ""} ${isTaken ? "taken" : ""}"
      data-preview-cartela="${number}"
      ${isPreviewed ? 'aria-current="true"' : ""}
      aria-label="${t("cartela_number_aria", { number })}${ariaSuffix}">${number}</button>`;
  }).join("");

  const confirm = $("confirm-cards-button");
  confirm.disabled = state.pendingSelections.length === 0 && state.ownedCards.length === 0;
  confirm.textContent = state.pendingSelections.length
    ? tn("buy_n_cartelas", state.pendingSelections.length)
    : state.ownedCards.length
      ? tn("view_my_cartelas", state.ownedCards.length)
      : t("buy_cartela_button");
  const watch = $("watch-cards-button");
  watch.disabled = state.pendingSelections.length === 0;
  watch.textContent = state.pendingSelections.length
    ? tn("watch_n_without_buying", state.pendingSelections.length)
    : t("choose_without_buying");
  const random = $("random-card-button");
  random.disabled = remainingSlots === 0;
  random.textContent = remainingSlots === 0
    ? t("max_cartelas_reached", { max: state.maximumCards })
    : t("preview_random_available");

  document.querySelectorAll("[data-preview-cartela]").forEach((button) => {
    button.addEventListener("click", () => previewCartela(Number(button.dataset.previewCartela)));
  });
  document.querySelectorAll("[data-remove-cartela]").forEach((button) => {
    button.addEventListener("click", () => {
      const number = Number(button.dataset.removeCartela);
      state.pendingSelections = state.pendingSelections.filter((item) => item !== number);
      if (state.previewCard?.card_number === number) {
        state.previewCard = null;
        state.previewCardNumber = null;
      }
      renderCardPicker();
      renderCartelaPreview();
    });
  });
}

async function previewCartela(cardNumber) {
  const previousNumber = state.previewCardNumber;
  state.previewCardNumber = cardNumber;
  highlightPreviewedCartela(cardNumber);
  try {
    const preview = await api(`/api/rooms/${state.pendingRoomId}/cards/${cardNumber}/preview`);
    if (state.previewCardNumber !== cardNumber) return;
    state.previewCard = preview;
    renderCartelaPreview();
  } catch (error) {
    if (state.previewCardNumber === cardNumber) {
      state.previewCardNumber = previousNumber;
      highlightPreviewedCartela(previousNumber);
    }
    toast(error.message);
  }
}

function highlightPreviewedCartela(cardNumber) {
  document.querySelectorAll("[data-preview-cartela]").forEach((button) => {
    const active = Number(button.dataset.previewCartela) === cardNumber;
    button.classList.toggle("previewing", active);
    if (active) button.setAttribute("aria-current", "true");
    else button.removeAttribute("aria-current");
  });
}

function renderCartelaPreview() {
  const panel = $("cartela-preview");
  if (!state.previewCard) {
    panel.classList.add("hidden");
    return;
  }
  const card = state.previewCard;
  const owned = state.ownedCards.includes(card.card_number);
  const selected = state.pendingSelections.includes(card.card_number);
  $("preview-title").textContent = t("cartela_hash", { number: card.card_number });
  $("preview-card-status").textContent = owned ? t("preview_status_owned") : selected ? t("preview_status_selected") : t("preview_status_ready");
  $("preview-card").innerHTML = card.numbers.flatMap((row) => row.map((number) =>
    `<span class="preview-cell ${number === 0 ? "free" : ""}">${number === 0 ? "F" : number}</span>`
  )).join("");
  const toggle = $("preview-toggle-button");
  toggle.disabled = owned;
  toggle.textContent = owned ? t("already_yours") : selected ? t("remove_this_cartela") : t("add_this_cartela");
  panel.classList.remove("hidden");
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function togglePreviewCartela() {
  if (!state.previewCard) return;
  const number = state.previewCard.card_number;
  if (state.ownedCards.includes(number)) return toast(t("toast_already_own_cartela"));
  if (state.pendingSelections.includes(number)) {
    state.pendingSelections = state.pendingSelections.filter((item) => item !== number);
  } else {
    const total = state.ownedCards.length + state.pendingSelections.length;
    if (total >= state.maximumCards) return toast(t("toast_choose_up_to_n_cartelas", { n: state.maximumCards }));
    state.pendingSelections.push(number);
  }
  renderCardPicker();
  renderCartelaPreview();
}

async function enterGame(roomId, cardNumbers = null) {
  const hasNewCards = Array.isArray(cardNumbers) && cardNumbers.length > 0;
  state.game = hasNewCards
    ? await api(`/api/rooms/${roomId}/join`, {
      method: "POST",
      body: JSON.stringify({ card_numbers: cardNumbers }),
    })
    : await api(`/api/rooms/${roomId}/game`);
  state.roomId = Number(roomId);
  state.pendingRoomId = null;
  $("card-modal").classList.add("hidden");
  setAvailableBalance(state.game.balance_santim);
  renderGame();
  showScreen("game-screen");
  connectSocket();
}

function confirmCartelas() {
  if (!state.pendingRoomId) return toast(t("toast_choose_a_game_first"));
  if (state.pendingSelections.length > 0) {
    return enterGame(state.pendingRoomId, [...state.pendingSelections]).catch((error) => toast(error.message));
  }
  if (state.ownedCards.length > 0) {
    return enterGame(state.pendingRoomId, null).catch((error) => toast(error.message));
  }
  toast(t("toast_choose_at_least_one"));
}

async function watchCartelas() {
  if (!state.pendingRoomId) return toast(t("toast_choose_a_game_first"));
  if (!state.pendingSelections.length) return toast(t("toast_choose_at_least_one_to_watch"));
  const roomId = state.pendingRoomId;
  const numbers = [...state.pendingSelections];
  try {
    const previews = await Promise.all(
      numbers.map((number) => api(`/api/rooms/${roomId}/cards/${number}/preview`))
    );
    if (!state.watchedCards[roomId]) state.watchedCards[roomId] = [];
    const existing = new Set(state.watchedCards[roomId].map((card) => card.card_number));
    let added = 0;
    previews.forEach((preview) => {
      if (existing.has(preview.card_number)) return;
      state.watchedCards[roomId].push({
        card_number: preview.card_number,
        numbers: preview.numbers,
        marks: [0],
        auto_mark: false,
      });
      added += 1;
    });
    state.pendingSelections = [];
    saveWatchedCards();
    await enterGame(roomId, null);
    toast(added ? tn("toast_watching_cartelas_free", added) : t("toast_already_watching"));
  } catch (error) { toast(error.message); }
}

function chooseRandomCard() {
  const candidates = state.availableCards.filter((number) => !state.pendingSelections.includes(number));
  if (!candidates.length) return toast(t("toast_no_more_cartelas_available"));
  const slotsUsed = state.ownedCards.length + state.pendingSelections.length;
  if (slotsUsed >= state.maximumCards) return toast(t("toast_choose_up_to_n_cartelas", { n: state.maximumCards }));
  const number = candidates[Math.floor(Math.random() * candidates.length)];
  previewCartela(number);
}

function announceResult(room, winners = []) {
  if (!room) return;
  const key = `${room.id}:${room.result_status}`;
  if (state.announcedResultKey === key) return;

  if (room.result_status === "disputed") {
    state.announcedResultKey = key;
    showResult(t("round_disputed_title"), t("result_round_disputed_copy", { id: room.id }), false);
    return;
  }
  if (room.result_status === "dismissed") {
    state.announcedResultKey = key;
    showResult(t("round_dismissed_title"), t("result_round_dismissed_copy"), false);
    return;
  }
  if (room.result_status === "settled") {
    state.announcedResultKey = key;
    const mine = winningsFor(state.user.telegram_id, winners);
    const myCartelaNumbers = mine.cards.map((card) => `#${card.card_number}`).join(", ");
    const winnersByPlayer = new Map();
    winners.forEach((item) => {
      const entry = winnersByPlayer.get(item.telegram_id) || { name: item.first_name, cardNumbers: [], payoutSantim: 0 };
      entry.cardNumbers.push(item.card_number);
      entry.payoutSantim += Number(item.payout_santim || 0);
      winnersByPlayer.set(item.telegram_id, entry);
    });
    const winnerSummaries = [...winnersByPlayer.values()]
      .map((entry) => t("winner_summary_entry", {
        name: entry.name,
        cards: entry.cardNumbers.map((number) => `#${number}`).join(", "),
        amount: money(entry.payoutSantim),
      }));
    showResult(
      mine.cards.length ? t("you_won") : winners.length > 1 ? t("split_winners_confirmed") : t("winner_confirmed"),
      mine.cards.length
        ? t(`result_you_won_copy_${mine.cards.length === 1 ? "one" : "other"}`, { numbers: myCartelaNumbers, amount: money(mine.payoutSantim) })
        : t("result_winners_same_call", { summaries: winnerSummaries.join(", ") }),
      mine.cards.length > 0,
      winners,
    );
  }
}

function renderGame() {
  if (!state.game) return;
  const { room, draws } = state.game;
  const cards = state.game.cards || (state.game.card ? [state.game.card] : []);
  const watchedCards = state.watchedCards[state.roomId] || [];
  const drawn = new Set(draws);
  setAvailableBalance(state.game.balance_santim ?? state.wallet?.balance_santim);
  announceResult(room, state.game.winners);

  if ($("room-name")) $("room-name").textContent = room.name;
  const playerCount = Number(room.unique_player_count || 0);
  if ($("player-count-text")) $("player-count-text").textContent = tn("players", playerCount);
  if ($("game-id-tag")) $("game-id-tag").textContent = t("round_hash", { id: room.id });
  if ($("game-status")) {
    const visibleStatus = room.result_status === "pending"
      ? t("status_pending_result")
      : room.result_status === "disputed" ? t("status_disputed") : t(`status_${room.state}`);
    $("game-status").textContent = visibleStatus;
    $("game-status").className = `status-dot ${room.result_status === "pending" ? "pending" : room.result_status === "disputed" ? "disputed" : room.state}`;
  }

  const resultPanel = $("result-review-panel");
  const reviewingResult = ["pending", "disputed"].includes(room.result_status);
  if (resultPanel) {
    resultPanel.classList.toggle("hidden", !reviewingResult);
    resultPanel.classList.toggle("disputed", room.result_status === "disputed");
    if (reviewingResult) {
      const winnerCount = Number(room.winner_count || state.game.winners?.length || 0);
      $("result-review-badge").textContent = room.result_status === "disputed" ? t("result_review_badge_disputed") : t("result_review_badge");
      $("result-review-title").textContent = room.result_status === "disputed"
        ? t("result_review_title_disputed", { id: room.id })
        : t("result_review_title");
      $("result-review-copy").textContent = room.result_status === "disputed"
        ? t("result_review_copy_disputed")
        : tn("result_review_winning_cartelas", winnerCount);
      $("result-review-countdown").textContent = room.result_status === "disputed" ? t("hold") : "15s";
      $("dispute-round-button").classList.toggle("hidden", room.result_status !== "pending" || cards.length === 0);
    }
  }

  const latest = draws.at(-1);
  if ($("latest-ball-container")) $("latest-ball-container").classList.toggle("hidden", !latest);
  if ($("latest-ball")) {
    const letter = latest ? "BINGO"[Math.floor((latest - 1) / 15)] : "";
    $("latest-ball").innerHTML = latest ? `<small>${letter}</small><strong>${latest}</strong>` : "—";
    $("latest-ball").className = `latest-ball ${latest ? ballClass(latest) : ""}`;
    $("latest-ball").setAttribute("aria-label", latest ? `Latest call ${letter} ${latest}` : t("no_calls_yet"));
  }

  if ($("recent-calls")) {
    $("recent-calls").innerHTML = [...draws].reverse().slice(1, 8)
      .map((num) => `<span class="call-chip ${ballClass(num)}">${label(num)}</span>`).join("");
  }

  if ($("game-pool")) $("game-pool").textContent = birr(room.stake_santim);
  if ($("cancel-all-button")) {
    $("cancel-all-button").classList.toggle("hidden", room.state !== "waiting" || cards.length < 2);
  }
  const buyCardButton = $("action-buy-card");
  const cardLimitReached = cards.length >= state.maximumCards;
  const buyingClosed = room.state === "running";
  if (buyCardButton) buyCardButton.disabled = cardLimitReached || buyingClosed;
  if ($("buy-card-label")) {
    $("buy-card-label").textContent = cardLimitReached
      ? t("buy_card_max_reached", { n: state.maximumCards })
      : buyingClosed ? t("buy_card_buying_closed") : t("buy_card_label");
  }
  if ($("buy-card-price")) {
    $("buy-card-price").textContent = cardLimitReached
      ? t("buy_card_limit_reached")
      : buyingClosed ? t("buy_card_round_started") : `🪙 ${money(room.stake_santim)}`;
  }

  const splitCount = Number(room.winner_count || 0);
  const displayedPayout = splitCount > 1 ? Math.floor(room.winner_payout_santim / splitCount) : room.winner_payout_santim;
  if ($("game-payout")) $("game-payout").textContent = money(displayedPayout);
  if ($("payout-currency")) $("payout-currency").textContent = splitCount > 1 ? t("birr_each") : t("birr_upper");

  const autoMarking = cards.length > 0 && cards.every((card) => card.auto_mark);
  if ($("card-label")) {
    $("card-label").textContent = cards.length && watchedCards.length
      ? t("card_label_bought_and_watching", { bought: cards.length, watching: watchedCards.length })
      : cards.length
        ? tn("card_label_your_cartelas", cards.length)
        : watchedCards.length
          ? tn("card_label_watching", watchedCards.length)
          : t("card_label_live_board");
  }
  if ($("your-cards-number")) $("your-cards-number").textContent = cards.length || 0;
  if ($("active-player-count")) $("active-player-count").textContent = playerCount;
  if ($("online-players-text")) $("online-players-text").textContent = tn("players_online", playerCount);

  if ($("auto-mark-toggle")) {
    $("auto-mark-toggle").checked = autoMarking;
    $("auto-mark-toggle").disabled = cards.length === 0;
    $("auto-mark-toggle").setAttribute("aria-label", autoMarking ? t("auto_marking_on") : t("auto_marking_off"));
  }
  if ($("mark-instruction")) {
    $("mark-instruction").textContent = cards.length === 0
      ? t("mark_instruction_no_cards")
      : autoMarking ? t("mark_instruction_auto") : t("mark_instruction_manual");
  }

  const winningCards = new Set((state.game.winners || []).map((winner) => winner.card_id));
  const cardsContainer = $("bingo-cards");
  if (cardsContainer) {
    cardsContainer.classList.toggle("multiple", cards.length + watchedCards.length > 1);
    const isWaiting = room.state === "waiting";
    const boughtHtml = cards.map((card) => {
      const blocked = Boolean(card.blocked);
      const verified = winningCards.has(card.id);
      const canClaim = room.state === "running" && !blocked && !verified;
      const claimLabel = blocked ? t("claim_blocked") : verified ? t("claim_verified") : isWaiting ? t("claim_cancel_this") : t("claim_bingo_button");
      const marks = new Set(card.marks || [0]);
      const cells = card.numbers.flatMap((row) => row.map((number) => {
        const free = number === 0;
        const marked = free || marks.has(number) || (card.auto_mark && drawn.has(number));
        const called = drawn.has(number);
        const formattedNum = free ? "F" : number < 10 ? "0" + number : number;
        return `<button class="bingo-cell ${free ? "free" : ""} ${called ? "called" : ""} ${marked ? "marked" : ""}"
          data-card-id="${card.id}" data-number="${number}" ${free || card.auto_mark || blocked ? "disabled" : ""}
          aria-label="${free ? t("free_space") : number}"><span>${formattedNum}</span></button>`;
      })).join("");
      const actionButton = isWaiting
        ? `<button type="button" class="cartela-claim-button cancel-variant" data-cancel-card="${card.id}">${claimLabel}</button>`
        : `<button type="button" class="cartela-claim-button" data-claim-card="${card.id}" ${canClaim ? "" : "disabled"}>${claimLabel}</button>`;
      return `<section class="cartela-board ${verified ? "winner" : ""} ${blocked ? "blocked" : ""}">
        <header><strong>${t("cartela_hash", { number: card.card_number })}</strong><span>${blocked ? t("cartela_blocked") : verified ? t("cartela_bingo") : t("draws_of_75", { n: draws.length })}</span></header>
        <div class="bingo-head" aria-hidden="true"><span>B</span><span>I</span><span>N</span><span>G</span><span>O</span></div>
        <div class="bingo-card">${cells}</div>
        ${actionButton}
      </section>`;
    }).join("");
    const watchedHtml = watchedCards.map((card) => {
      const wouldWin = watchCardWouldWin(card.numbers, drawn);
      const marks = new Set(card.marks || [0]);
      const cells = card.numbers.flatMap((row) => row.map((number) => {
        const free = number === 0;
        const marked = free || marks.has(number);
        const called = drawn.has(number);
        const formattedNum = free ? "F" : number < 10 ? "0" + number : number;
        return `<button class="bingo-cell ${free ? "free" : ""} ${called ? "called" : ""} ${marked ? "marked" : ""}"
          data-watch-number="${card.card_number}" data-cell-number="${number}" ${free ? "disabled" : ""}
          aria-label="${free ? t("free_space") : number}"><span>${formattedNum}</span></button>`;
      })).join("");
      return `<section class="cartela-board watching ${wouldWin ? "winner" : ""}">
        <header><strong>${t("cartela_hash", { number: card.card_number })}</strong><span>${wouldWin ? t("watching_would_win") : t("watching_progress", { n: draws.length })}</span></header>
        <div class="bingo-head" aria-hidden="true"><span>B</span><span>I</span><span>N</span><span>G</span><span>O</span></div>
        <div class="bingo-card">${cells}</div>
        <button type="button" class="cartela-claim-button cancel-variant" data-remove-watch="${card.card_number}">${t("stop_watching")}</button>
      </section>`;
    }).join("");
    cardsContainer.innerHTML = (boughtHtml + watchedHtml) || `<div class="watching-state">${t("watch_no_cartela")}</div>`;
  }

  document.querySelectorAll(".bingo-cell[data-card-id]").forEach((cell) => {
    cell.addEventListener("click", (event) => {
      event.stopPropagation();
      markNumber(Number(cell.dataset.cardId), Number(cell.dataset.number));
    });
  });
  document.querySelectorAll(".bingo-cell[data-watch-number]").forEach((cell) => {
    cell.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleWatchMark(state.roomId, Number(cell.dataset.watchNumber), Number(cell.dataset.cellNumber));
    });
  });
  document.querySelectorAll("[data-remove-watch]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      removeWatchedCard(state.roomId, Number(button.dataset.removeWatch));
    });
  });
  document.querySelectorAll("[data-claim-card]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      claimBingo(Number(button.dataset.claimCard));
    });
  });
  document.querySelectorAll("[data-cancel-card]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      cancelOneCard(Number(button.dataset.cancelCard));
    });
  });
}

async function cancelCards(cardIds) {
  try {
    state.game = await api(`/api/rooms/${state.roomId}/cards/cancel`, {
      method: "POST",
      body: JSON.stringify({ card_ids: cardIds }),
    });
    tg?.HapticFeedback?.notificationOccurred("success");
    renderGame();
    toast(cardIds && cardIds.length === 1 ? t("toast_cartela_cancelled") : t("toast_cartelas_cancelled"));
  } catch (error) { toast(error.message); }
}

async function cancelOneCard(cardId) {
  const card = (state.game.cards || []).find((item) => item.id === cardId);
  const title = card ? t("cancel_one_cartela_title", { number: card.card_number }) : t("cancel_one_cartela_generic_title");
  const confirmed = await showConfirm(
    t("cancel_one_cartela_copy"),
    { title, confirmLabel: t("cancel_one_cartela_confirm") },
  );
  if (!confirmed) return;
  cancelCards([cardId]);
}

async function cancelAllCartelas() {
  const count = (state.game.cards || []).length;
  if (!count) return;
  const confirmed = await showConfirm(
    t("cancel_all_cartelas_copy"),
    { title: t("cancel_all_cartelas_title", { n: count }), confirmLabel: t("cancel_all_confirm") },
  );
  if (!confirmed) return;
  cancelCards(null);
}

async function markNumber(cardId, number) {
  if (!state.game.draws.includes(number)) {
    tg?.HapticFeedback?.notificationOccurred("error");
    return toast(t("toast_wait_for_number"));
  }
  try {
    const updated = await api(`/api/rooms/${state.roomId}/mark`, { method: "POST", body: JSON.stringify({ card_id: cardId, number }) });
    state.game.cards = (state.game.cards || []).map((card) => card.id === updated.id ? updated : card);
    state.game.card = state.game.cards[0] || null;
    tg?.HapticFeedback?.impactOccurred("light");
    renderGame();
  } catch (error) { toast(error.message); }
}

async function setAutoMark(enabled) {
  try {
    state.game.cards = await api(`/api/rooms/${state.roomId}/mode`, { method: "POST", body: JSON.stringify({ enabled }) });
    state.game.card = state.game.cards[0] || null;
    renderGame();
  } catch (error) {
    $("auto-mark-toggle").checked = !enabled;
    toast(error.message);
  }
}

async function claimBingo(cardId) {
  try {
    const result = await api(`/api/rooms/${state.roomId}/claim`, {
      method: "POST", body: JSON.stringify({ card_id: cardId }),
    });
    if (result.accepted) {
      tg?.HapticFeedback?.notificationOccurred("success");
      if (result.outcome === "winner") {
        await refreshGame();
        const mine = winningsFor(state.user.telegram_id, state.game.winners);
        showResult(t("you_won"), t("result_your_payout", { amount: money(mine.payoutSantim) }), true, mine.cards);
      } else {
        toast(t("toast_bingo_recorded"));
      }
    } else {
      tg?.HapticFeedback?.notificationOccurred("error");
      if (result.card) {
        state.game.cards = state.game.cards.map((card) => card.id === result.card.id ? result.card : card);
        state.game.card = state.game.cards[0] || null;
      }
      renderGame();
      toast(t("toast_wrong_bingo", { number: result.card?.card_number || "" }));
    }
  } catch (error) { toast(error.message); }
}

async function disputeRound() {
  if (state.game?.room?.result_status !== "pending") return;
  const reason = window.prompt(t("dispute_prompt_message"), "");
  if (reason === null) return;
  const button = $("dispute-round-button");
  button.disabled = true;
  try {
    const outcome = await api(`/api/rooms/${state.roomId}/dispute`, {
      method: "POST",
      body: JSON.stringify({ reason: reason.trim() || t("dispute_default_reason") }),
    });
    state.game.room = outcome.room;
    state.game.winners = outcome.winners;
    state.game.disputed_by_me = true;
    renderGame();
  } catch (error) {
    toast(error.message);
    button.disabled = false;
  }
}

function connectSocket() {
  disconnectSocket();
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  state.socket = new WebSocket(`${protocol}://${location.host}/ws/rooms/${state.roomId}?token=${encodeURIComponent(state.token)}`);
  state.socket.addEventListener("message", ({ data }) => handleEvent(JSON.parse(data)));
  state.socket.addEventListener("close", () => { if (state.roomId) setTimeout(refreshGame, 1800); });
  state.pingTimer = setInterval(() => {
    if (state.socket?.readyState === WebSocket.OPEN) state.socket.send("ping");
  }, 20000);
}

function disconnectSocket() {
  clearInterval(state.pingTimer);
  if (state.socket) {
    state.socket.onclose = null;
    state.socket.close();
    state.socket = null;
  }
}

async function refreshGame() {
  if (!state.roomId) return;
  try {
    state.game = await api(`/api/rooms/${state.roomId}/game`);
    renderGame();
    if (!state.socket || state.socket.readyState === WebSocket.CLOSED) connectSocket();
  } catch (_) {}
}

function handleEvent(event) {
  if (event.type === "connected") state.game = event.state;
  if (event.type === "game_started") state.game.room = event.room;
  if (event.type === "player_joined") state.game.room = event.room;
  if (event.type === "cards_cancelled") state.game.room = event.room;
  if (event.type === "number_called") {
    state.game.draws = event.draws;
    tg?.HapticFeedback?.impactOccurred("light");
  }
  if (event.type === "winner") {
    state.game.room = event.room;
    const mine = event.room.winner_telegram_id === state.user.telegram_id;
    showResult(mine ? t("you_won") : t("we_have_a_winner"), mine
      ? t("result_your_payout", { amount: money(event.room.winner_payout_santim) })
      : t("result_winner_first_copy", { name: event.winner_name }), mine);
  }
  if (event.type === "bingo_pending") {
    state.game.room = event.room;
    state.game.winners = event.winners;
    toast(tn("toast_bingo_cards_detected", event.winners.length));
  }
  if (event.type === "game_disputed") {
    state.game.room = event.room;
    state.game.winners = event.winners;
  }
  if (event.type === "game_settled") {
    state.game.room = event.room;
    state.game.winners = event.winners;
    refreshAvailableBalance();
  }
  if (event.type === "game_dismissed") {
    state.game.room = event.room;
    state.game.winners = event.winners;
    refreshAvailableBalance();
  }
  if (event.type === "game_finished") state.game.room = event.room;
  renderGame();
}

function renderResultWinnerCards(winners, drawnNumbers) {
  const container = $("result-winner-cards");
  if (!container) return;
  const drawn = new Set(drawnNumbers || []);
  container.innerHTML = (winners || []).map((winner) => {
    const marks = new Set([0]);
    const cells = (winner.numbers || []).flatMap((row) => row.map((number) => {
      const free = number === 0;
      const called = free || drawn.has(number);
      const formattedNum = free ? "F" : number < 10 ? "0" + number : number;
      return `<button class="bingo-cell ${free ? "free" : ""} ${called ? "called marked" : ""}" disabled
        aria-label="${free ? t("free_space") : number}"><span>${formattedNum}</span></button>`;
    })).join("");
    return `<section class="cartela-board winner result-winner-card">
      <header><strong>${escapeHtml(winner.first_name)}</strong><span>${t("cartela_hash", { number: winner.card_number })}</span></header>
      <div class="result-winner-card-payout">${t("result_winner_card_payout", { amount: money(winner.payout_santim) })}</div>
      <div class="bingo-head" aria-hidden="true"><span>B</span><span>I</span><span>N</span><span>G</span><span>O</span></div>
      <div class="bingo-card">${cells}</div>
    </section>`;
  }).join("");
}

function showResult(title, copy, won, winnerCards = []) {
  $("result-icon").textContent = won ? "★" : "L";
  $("result-title").textContent = title;
  $("result-copy").textContent = copy;
  renderResultWinnerCards(winnerCards, state.game?.draws);
  $("result-modal").classList.remove("hidden");
}

function showConfirm(message, { title = "Please confirm", confirmLabel = "Confirm" } = {}) {
  return new Promise((resolve) => {
    $("confirm-title").textContent = title;
    $("confirm-copy").textContent = message;
    $("confirm-ok-button").textContent = confirmLabel;
    $("confirm-modal").classList.remove("hidden");

    const okButton = $("confirm-ok-button");
    const cancelButton = $("confirm-cancel-button");
    const finish = (result) => {
      $("confirm-modal").classList.add("hidden");
      okButton.removeEventListener("click", onConfirm);
      cancelButton.removeEventListener("click", onCancel);
      resolve(result);
    };
    const onConfirm = () => finish(true);
    const onCancel = () => finish(false);
    okButton.addEventListener("click", onConfirm);
    cancelButton.addEventListener("click", onCancel);
  });
}

function selectWalletTab(tab) {
  const selected = ["deposit", "withdraw", "transfer", "history"].includes(tab) ? tab : "deposit";
  document.querySelectorAll("[data-wallet-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.walletTab === selected);
  });
  $("wallet-deposit-panel").classList.toggle("hidden", selected !== "deposit");
  $("wallet-withdraw-panel").classList.toggle("hidden", selected !== "withdraw");
  $("wallet-transfer-panel").classList.toggle("hidden", selected !== "transfer");
  $("wallet-history-panel").classList.toggle("hidden", selected !== "history");
}

async function openWallet(tab = "deposit") {
  try {
    const [wallet, instructions] = await Promise.all([api("/api/wallet"), api("/api/payment-instructions")]);
    state.wallet = wallet;
    $("modal-balance").textContent = birr(wallet.balance_santim);
    $("wallet-balance").textContent = birr(wallet.balance_santim);
    $("modal-bonus-note").classList.toggle("hidden", !wallet.bonus_santim);
    if (wallet.bonus_santim) {
      $("modal-bonus-note").textContent = t("wallet_bonus_note", { amount: money(wallet.bonus_santim) });
    }
    $("telebirr-account").textContent = instructions.telebirr_account || t("not_configured");
    $("cbe-account").textContent = instructions.cbe_birr_account || t("not_configured");
    $("cbe-bank-account").textContent = instructions.cbe_bank_account || t("not_configured");
    $("telebirr-account-name").textContent = t("account_name_label", { name: instructions.telebirr_account_name });
    $("cbe-account-name").textContent = t("account_name_label", { name: instructions.cbe_account_name });
    $("deposit-amount").min = money(instructions.minimum_deposit_santim);
    $("deposit-amount").placeholder = t("deposit_amount_placeholder", { min: money(instructions.minimum_deposit_santim) });
    $("withdraw-available").textContent = birr(wallet.withdrawable_balance_santim);
    $("withdraw-bonus-locked").textContent = birr(wallet.bonus_santim);
    $("withdraw-reserved").textContent = birr(wallet.reserved_withdrawal_santim);
    $("withdrawal-rule").textContent = t("withdrawal_rule", { amount: money(instructions.minimum_withdrawal_santim) });
    $("withdrawal-amount").min = money(instructions.minimum_withdrawal_santim);
    if (!$("withdrawal-name").value) $("withdrawal-name").value = state.user.first_name;
    const belowMinimum = wallet.withdrawable_balance_santim < instructions.minimum_withdrawal_santim;
    $("withdrawal-blocked-notice").classList.toggle("hidden", !belowMinimum);
    if (belowMinimum) {
      const shortfall = money(instructions.minimum_withdrawal_santim - wallet.withdrawable_balance_santim);
      $("withdrawal-blocked-notice").innerHTML = t("withdrawal_blocked_notice", {
        balance: money(wallet.withdrawable_balance_santim),
        minimum: money(instructions.minimum_withdrawal_santim),
        shortfall,
      });
    }
    $("withdrawal-amount").disabled = belowMinimum;
    $("withdrawal-account").disabled = belowMinimum;
    $("withdrawal-name").disabled = belowMinimum;
    $("withdrawal-submit-button").disabled = belowMinimum;
    $("withdrawal-submit-button").textContent = belowMinimum ? t("balance_too_low_to_withdraw") : t("request_withdrawal");
    $("my-telegram-id").textContent = state.user.telegram_id;
    $("transfer-available").textContent = birr(wallet.withdrawable_balance_santim);
    $("transfer-bonus-locked").textContent = birr(wallet.bonus_santim);
    $("transfer-rule").textContent = t("transfer_rule", { amount: money(state.config.minimum_transfer_santim) });
    $("transfer-amount").min = money(state.config.minimum_transfer_santim);
    $("money-mode-note").textContent = wallet.real_money_enabled
      ? t("money_mode_note_real")
      : t("money_mode_note_test");
    const deposits = wallet.deposits.map((item) => ({
      label: t("activity_deposit_status", { provider: providerLabel(item.provider), status: t(`status_${item.status}`) }),
      displayAmount: item.status === "approved" ? `+${birr(item.amount_santim)}` : `${birr(item.amount_santim)} ${t(`status_${item.status}`)}`,
      positive: item.status === "approved",
      date: item.submitted_at,
    }));
    const withdrawals = wallet.withdrawals.map((item) => ({
      label: t("activity_withdrawal_status", { provider: providerLabel(item.provider), status: t(`status_${item.status}`) }),
      displayAmount: item.status === "rejected" ? t("amount_released", { amount: money(item.amount_santim) }) : `-${birr(item.amount_santim)}${item.status === "pending" ? t("amount_reserved_suffix") : ""}`,
      positive: item.status === "rejected",
      date: item.submitted_at,
    }));
    const transfers = (wallet.transfers || []).map((item) => ({
      label: item.direction === "sent"
        ? t("activity_transfer_sent", { name: item.recipient_first_name, status: t(`status_${item.status}`) })
        : t("activity_transfer_received", { name: item.sender_first_name, status: t(`status_${item.status}`) }),
      displayAmount: item.direction === "sent"
        ? (item.status === "rejected" ? t("amount_released", { amount: money(item.amount_santim) }) : `-${birr(item.amount_santim)}${item.status === "pending" ? t("amount_reserved_suffix") : ""}`)
        : (item.status === "approved" ? `+${birr(item.amount_santim)}` : `${birr(item.amount_santim)} ${t(`status_${item.status}`)}`),
      positive: item.direction === "sent" ? item.status === "rejected" : item.status === "approved",
      date: item.submitted_at,
    }));
    const entries = wallet.entries.filter((item) => !["deposit", "withdrawal", "transfer"].includes(item.reference_type)).map((item) => ({
      label: item.description,
      displayAmount: `${item.amount_santim > 0 ? "+" : ""}${birr(item.amount_santim)}`,
      positive: item.amount_santim > 0,
      date: item.created_at,
    }));
    state.walletHistoryActivity = [...deposits, ...withdrawals, ...transfers, ...entries]
      .sort((left, right) => new Date(right.date) - new Date(left.date));
    state.walletHistoryPage = 1;
    renderWalletHistoryPage();
    selectWalletTab(tab);
    $("wallet-modal").classList.remove("hidden");
  } catch (error) { toast(error.message); }
}

const WALLET_HISTORY_PAGE_SIZE = 8;

function renderWalletHistoryPage() {
  const activity = state.walletHistoryActivity || [];
  const totalPages = Math.max(1, Math.ceil(activity.length / WALLET_HISTORY_PAGE_SIZE));
  state.walletHistoryPage = Math.min(Math.max(1, state.walletHistoryPage || 1), totalPages);
  const start = (state.walletHistoryPage - 1) * WALLET_HISTORY_PAGE_SIZE;
  const page = activity.slice(start, start + WALLET_HISTORY_PAGE_SIZE);

  $("wallet-history").innerHTML = page.map((item) => `
    <div class="history-row"><span>${escapeHtml(item.label)}</span>
      <strong class="${item.positive ? "credit" : "debit"}">${escapeHtml(item.displayAmount)}</strong>
      <small>${new Date(item.date).toLocaleString()}</small></div>`).join("") || `<div class="empty-state">${t("no_transactions_yet")}</div>`;

  $("wallet-history-pagination").classList.toggle("hidden", activity.length <= WALLET_HISTORY_PAGE_SIZE);
  $("wallet-history-page-status").textContent = t("page_of", { page: state.walletHistoryPage, total: totalPages });
  $("wallet-history-prev").disabled = state.walletHistoryPage <= 1;
  $("wallet-history-next").disabled = state.walletHistoryPage >= totalPages;
}

async function submitDeposit(event) {
  event.preventDefault();
  const amount = Number($("deposit-amount").value);
  const minimum = Number(state.config.minimum_deposit_santim || 1_000) / 100;
  if (!Number.isFinite(amount) || amount < minimum) return toast(t("toast_deposit_minimum", { amount: minimum.toFixed(2) }));
  try {
    const deposit = await api("/api/deposits", {
      method: "POST",
      body: JSON.stringify({
        provider: $("deposit-provider").value,
        amount_santim: Math.round(amount * 100),
        transaction_id: $("deposit-txid").value.trim(),
      }),
    });
    $("deposit-form").reset();
    toast(t("toast_deposit_submitted", { id: deposit.id }));
    await openWallet("history");
  } catch (error) { toast(error.message); }
}

async function submitWithdrawal(event) {
  event.preventDefault();
  const amount = Number($("withdrawal-amount").value);
  const minimum = Number(state.config.minimum_withdrawal_santim || 10_000) / 100;
  const available = Number(state.wallet?.withdrawable_balance_santim || 0) / 100;
  if (available < minimum) {
    return toast(t("toast_withdraw_below_minimum", { minimum: minimum.toFixed(2), available: available.toFixed(2) }));
  }
  if (!$("withdrawal-amount").value || !Number.isFinite(amount)) {
    return toast(t("toast_withdraw_enter_amount"));
  }
  if (amount < minimum) {
    return toast(t("toast_withdraw_minimum", { minimum: minimum.toFixed(2) }));
  }
  if (amount > available) {
    return toast(t("toast_withdraw_insufficient", { available: available.toFixed(2) }));
  }
  try {
    const withdrawal = await api("/api/withdrawals", {
      method: "POST",
      body: JSON.stringify({
        provider: $("withdrawal-provider").value,
        amount_santim: Math.round(amount * 100),
        account_number: $("withdrawal-account").value.trim(),
        account_name: $("withdrawal-name").value.trim(),
      }),
    });
    $("withdrawal-form").reset();
    toast(t("toast_withdrawal_submitted", { id: withdrawal.id }));
    await openWallet("history");
  } catch (error) { toast(error.message); }
}

let transferRecipientLookupToken = 0;

async function lookupTransferRecipient() {
  const preview = $("transfer-recipient-preview");
  const submitButton = $("transfer-submit-button");
  const raw = $("transfer-recipient-id").value.trim();
  const telegramId = Number(raw);
  state.transferRecipient = null;
  submitButton.disabled = true;
  submitButton.textContent = t("find_recipient_first");

  if (!raw || !Number.isFinite(telegramId)) {
    preview.classList.add("hidden");
    return;
  }
  if (telegramId === state.user.telegram_id) {
    preview.className = "transfer-recipient-preview not-found";
    preview.textContent = t("transfer_cannot_send_to_self");
    return;
  }

  const token = ++transferRecipientLookupToken;
  try {
    const recipient = await api(`/api/users/lookup/${telegramId}`);
    if (token !== transferRecipientLookupToken) return;
    state.transferRecipient = recipient;
    preview.className = "transfer-recipient-preview found";
    preview.textContent = t("transfer_sending_to", {
      name: recipient.first_name,
      username: recipient.username ? ` (@${recipient.username})` : "",
    });
    preview.classList.remove("hidden");
    submitButton.disabled = false;
    submitButton.textContent = t("request_transfer");
  } catch (error) {
    if (token !== transferRecipientLookupToken) return;
    preview.className = "transfer-recipient-preview not-found";
    preview.textContent = error.message;
    preview.classList.remove("hidden");
  }
}

async function submitTransfer(event) {
  event.preventDefault();
  const amount = Number($("transfer-amount").value);
  const minimum = Number(state.config.minimum_transfer_santim || 1_000) / 100;
  const available = Number(state.wallet?.withdrawable_balance_santim || 0) / 100;
  if (!state.transferRecipient) return toast(t("toast_transfer_no_recipient"));
  if (!Number.isFinite(amount) || amount < minimum) return toast(t("toast_transfer_minimum", { minimum: minimum.toFixed(2) }));
  if (amount > available) return toast(t("toast_transfer_insufficient", { available: available.toFixed(2) }));
  try {
    const transfer = await api("/api/transfers", {
      method: "POST",
      body: JSON.stringify({
        recipient_telegram_id: state.transferRecipient.telegram_id,
        amount_santim: Math.round(amount * 100),
      }),
    });
    $("transfer-form").reset();
    $("transfer-recipient-preview").classList.add("hidden");
    state.transferRecipient = null;
    toast(t("toast_transfer_submitted", { id: transfer.id }));
    await openWallet("history");
  } catch (error) { toast(error.message); }
}

function shareRoom() {
  const link = state.config.bot_username
    ? `https://t.me/${state.config.bot_username}?startapp=room_${state.roomId}`
    : `${location.origin}/?startapp=room_${state.roomId}`;
  if (!state.config.bot_username) {
    navigator.clipboard?.writeText(link);
    return toast(t("toast_invite_link_copied"));
  }
  const share = `https://t.me/share/url?url=${encodeURIComponent(link)}&text=${encodeURIComponent(t("share_room_message"))}`;
  tg?.openTelegramLink ? tg.openTelegramLink(share) : window.open(share, "_blank");
}

function label(number) {
  return `${"BINGO"[Math.floor((number - 1) / 15)]}${number}`;
}

function watchCardWinningLines(numbers) {
  const lines = numbers.map((row) => new Set(row));
  for (let column = 0; column < 5; column++) lines.push(new Set(numbers.map((row) => row[column])));
  lines.push(new Set(numbers.map((row, index) => row[index])));
  lines.push(new Set(numbers.map((row, index) => row[4 - index])));
  lines.push(new Set([numbers[0][0], numbers[0][4], numbers[4][0], numbers[4][4]]));
  return lines;
}

function watchCardWouldWin(numbers, drawn) {
  const eligible = new Set([...drawn, 0]);
  return watchCardWinningLines(numbers).some((line) => [...line].every((value) => eligible.has(value)));
}

function toggleWatchMark(roomId, cardNumber, number) {
  if (!state.game?.draws?.includes(number)) {
    tg?.HapticFeedback?.notificationOccurred("error");
    return toast(t("toast_wait_for_number"));
  }
  const card = (state.watchedCards[roomId] || []).find((item) => item.card_number === cardNumber);
  if (!card) return;
  const marks = new Set(card.marks || [0]);
  if (marks.has(number)) marks.delete(number); else marks.add(number);
  marks.add(0);
  card.marks = [...marks].sort((left, right) => left - right);
  saveWatchedCards();
  tg?.HapticFeedback?.impactOccurred("light");
  renderGame();
}

function removeWatchedCard(roomId, cardNumber) {
  const list = state.watchedCards[roomId];
  if (!list) return;
  state.watchedCards[roomId] = list.filter((card) => card.card_number !== cardNumber);
  saveWatchedCards();
  tg?.HapticFeedback?.impactOccurred("light");
  renderGame();
  toast(t("toast_stopped_watching", { number: cardNumber }));
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

$("refresh-button").addEventListener("click", loadLobby);
document.querySelectorAll("[data-locale-option]").forEach((button) => {
  button.addEventListener("click", () => setLocale(button.dataset.localeOption));
});
$("profile-button").addEventListener("click", () => {
  const handle = state.user.username ? `@${state.user.username}` : t("telegram_player_fallback");
  toast(`${state.user.first_name} · ${handle}`);
});
$("wallet-button").addEventListener("click", () => openWallet("deposit"));
$("withdraw-button").addEventListener("click", () => openWallet("withdraw"));
$("rules-button").addEventListener("click", () => $("rules-modal").classList.remove("hidden"));
$("close-rules-modal").addEventListener("click", () => $("rules-modal").classList.add("hidden"));
$("rules-done-button").addEventListener("click", () => $("rules-modal").classList.add("hidden"));
$("close-wallet-modal").addEventListener("click", () => $("wallet-modal").classList.add("hidden"));
$("deposit-form").addEventListener("submit", submitDeposit);
$("withdrawal-form").addEventListener("submit", submitWithdrawal);
$("transfer-form").addEventListener("submit", submitTransfer);
$("copy-my-telegram-id").addEventListener("click", async () => {
  const id = String(state.user?.telegram_id || "");
  try {
    await navigator.clipboard.writeText(id);
    toast(t("toast_telegram_id_copied"));
  } catch (_) {
    toast(t("toast_your_telegram_id", { id }));
  }
});
let transferLookupDebounce;
$("transfer-recipient-id").addEventListener("input", () => {
  clearTimeout(transferLookupDebounce);
  transferLookupDebounce = setTimeout(lookupTransferRecipient, 400);
});
$("wallet-history-prev").addEventListener("click", () => {
  state.walletHistoryPage -= 1;
  renderWalletHistoryPage();
});
$("wallet-history-next").addEventListener("click", () => {
  state.walletHistoryPage += 1;
  renderWalletHistoryPage();
});
document.querySelectorAll("[data-wallet-tab]").forEach((button) => {
  button.addEventListener("click", () => selectWalletTab(button.dataset.walletTab));
});
$("preview-toggle-button").addEventListener("click", togglePreviewCartela);
$("confirm-cards-button").addEventListener("click", confirmCartelas);
$("watch-cards-button").addEventListener("click", watchCartelas);
$("random-card-button").addEventListener("click", chooseRandomCard);
$("close-card-modal").addEventListener("click", () => $("card-modal").classList.add("hidden"));
$("back-button").addEventListener("click", () => {
  state.roomId = null;
  loadLobby().catch((error) => toast(error.message));
});
$("share-button").addEventListener("click", shareRoom);
$("auto-mark-toggle").addEventListener("change", (event) => setAutoMark(event.target.checked));
$("action-buy-card")?.addEventListener("click", () => openCardChooser(state.roomId));
$("cancel-all-button")?.addEventListener("click", cancelAllCartelas);
$("dispute-round-button")?.addEventListener("click", disputeRound);
$("manage-cards-button")?.addEventListener("click", () => openCardChooser(state.roomId));
$("view-all-calls")?.addEventListener("click", () => {
  const calls = state.game?.draws?.map(label).join(" · ");
  toast(calls ? t("calls_list", { calls }) : t("no_calls_yet"));
});
$("result-button").addEventListener("click", () => {
  $("result-modal").classList.add("hidden");
  state.roomId = null;
  loadLobby().catch((error) => toast(error.message));
});

setInterval(refreshCountdowns, 1000);

async function boot() {
  applyStaticTranslations();
  try {
    tg?.ready();
    tg?.expand();
    tg?.setHeaderColor?.("#f5f0e7");
    tg?.setBackgroundColor?.("#f5f0e7");
    const invitedRoom = await authenticate();
    await loadLobby();
    if (state.signupBonusSantim > 0) {
      toast(t("toast_welcome_bonus", { amount: money(state.signupBonusSantim) }));
    }
    if (state.pendingWalletTab) await openWallet(state.pendingWalletTab);
    if (invitedRoom) {
      const room = state.rooms.find((item) => item.id === invitedRoom);
      const hasWatchedCards = (state.watchedCards[invitedRoom] || []).length > 0;
      if (room?.state === "running" || hasWatchedCards) await enterGame(invitedRoom, null);
      else if (room) await openCardChooser(invitedRoom);
    }
  } catch (error) {
    $("error-copy").textContent = error.message;
    showScreen("error-screen");
  }
}

boot();
