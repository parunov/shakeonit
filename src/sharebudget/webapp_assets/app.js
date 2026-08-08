"use strict";

const tg = window.Telegram?.WebApp;
const app = document.getElementById("app");
const title = document.getElementById("page-title");
const avatar = document.getElementById("avatar");
const nav = document.getElementById("bottom-nav");
const sheetLayer = document.getElementById("sheet-layer");
const sheet = document.getElementById("sheet");
const toastNode = document.getElementById("toast");
const historyBadge = document.getElementById("history-badge");
const botUsername = document.querySelector('meta[name="telegram-bot-username"]')?.content;
const launchParams = new URLSearchParams(window.location.search);
const moneyFormatters = new Map();
const dateFormatter = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });

const state = {
  bootstrap: null,
  collection: null,
  collectionReturn: "collections",
  collectionTab: "overview",
  details: new Map(),
  nav: "collections",
  busy: false,
  syncVersion: null,
  syncTimer: null,
  lastSyncCheck: 0,
  refreshInFlight: false,
  balanceMode: "collections",
  balanceData: null,
  globalHistory: null,
  collectionHistoryLimit: 20,
  collectionEventsLimit: 20,
  viewStack: [],
  swipeStart: null,
  collectionSwipe: null,
  quickPayExpanded: false,
  promptedRepayments: new Set(),
  launchIntent: launchParams.get("intent")
    || launchParams.get("tgWebAppStartParam")
    || tg?.initDataUnsafe?.start_param,
};

const e = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

function money(amount, currency) {
  if (!moneyFormatters.has(currency)) {
    moneyFormatters.set(currency, new Intl.NumberFormat("ru-RU", {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
    }));
  }
  return moneyFormatters.get(currency).format((amount || 0) / 100);
}

function moneyMap(values) {
  const entries = Object.entries(values || {}).filter(([, amount]) => amount);
  return entries.length
    ? entries.map(([currency, amount]) => money(amount, currency)).join(" · ")
    : "0";
}

function userLink(id, fullName, username = "") {
  if (!id || !fullName) return e(fullName || "");
  return `<button class="user-link" type="button" data-action="open-user" data-user-id="${Number(id)}" data-username="${e(username || "")}">${e(fullName)}</button>`;
}

function openTelegramUser(target) {
  const username = target.dataset.username;
  const url = username
    ? `https://t.me/${encodeURIComponent(username.replace(/^@/, ""))}`
    : `https://t.me/${encodeURIComponent(botUsername)}?start=contact_${encodeURIComponent(target.dataset.userId)}`;
  haptic();
  if (!username) toast("Открываю контакт через бота");
  if (tg?.openTelegramLink) tg.openTelegramLink(url);
  else window.location.href = url;
}

function debtTotals(debts, key) {
  return (debts || []).reduce((result, debt) => {
    if (debt[key] === state.bootstrap.user.id) {
      result[debt.currency] = (result[debt.currency] || 0) + debt.amount;
    }
    return result;
  }, {});
}

function balanceStatus(amount) {
  if (amount > 0) return { icon: "💰", label: "ожидаете возврат" };
  if (amount < 0) return { icon: "💸", label: "нужно вернуть долг" };
  return { icon: "🤝", label: "все расчёты закрыты" };
}

function shortDate(value) {
  if (!value) return "";
  const normalized = value.includes("T") ? value : `${value.replace(" ", "T")}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime())
    ? value.slice(0, 16)
    : dateFormatter.format(date);
}

function haptic(type = "light") {
  tg?.HapticFeedback?.impactOccurred(type);
}

function toast(message, isError = false) {
  toastNode.textContent = message;
  toastNode.classList.toggle("error", isError);
  toastNode.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { toastNode.hidden = true; }, 2600);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Init-Data": tg?.initData || "",
      ...(options.headers || {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.ok) throw new Error(data.error || "Не удалось выполнить действие");
  return data;
}

function setBusy(button, busy) {
  state.busy = busy;
  if (button) {
    button.disabled = busy;
    if (busy) {
      button.dataset.oldText = button.textContent;
      button.textContent = "Сохраняем…";
    } else if (button.dataset.oldText) {
      button.textContent = button.dataset.oldText;
    }
  }
}

function showSheet(html) {
  sheet.innerHTML = html;
  sheetLayer.hidden = false;
  tg?.enableClosingConfirmation?.();
}

function closeSheet() {
  sheetLayer.hidden = true;
  sheet.replaceChildren();
  tg?.disableClosingConfirmation?.();
}

function reportToast(result, fallback = "Готово") {
  haptic("light");
  if (result.notifications_queued) {
    toast(`${fallback} · уведомления отправляются`);
    return;
  }
  if (result.notifications_sent > 0) {
    toast(`${fallback} · подписчики уведомлены: ${result.notifications_sent}`);
  } else {
    toast(result.report_sent === false ? fallback : `${fallback} · отчёт отправлен в чат`);
  }
}

function showPendingRepaymentConfirmation() {
  const item = state.bootstrap?.pending_repayment_confirmation;
  if (!item || !sheetLayer.hidden || state.promptedRepayments.has(Number(item.id))) return false;
  state.promptedRepayments.add(Number(item.id));
  const comment = item.comment
    ? `<div class="repayment-prompt-note"><small>Комментарий</small><span>${e(item.comment)}</span></div>`
    : "";
  showSheet(`
    <div class="repayment-prompt-mark" aria-hidden="true">↙</div>
    <h2>Подтвердите получение</h2>
    <p class="sheet-intro">Проверьте перевод — после подтверждения баланс сбора обновится.</p>
    <section class="repayment-prompt-card">
      <div><small>Сумма возврата</small><strong>${money(item.amount, item.currency)}</strong></div>
      <div class="repayment-prompt-row"><small>От кого</small><span>${userLink(item.creator_id, item.creator_name, item.creator_username)}</span></div>
      <div class="repayment-prompt-row"><small>Сбор</small><span>${e(item.collection_title)}</span></div>
      ${comment}
    </section>
    <div class="sheet-actions repayment-prompt-actions">
      <button class="primary-button" type="button" data-action="prompt-confirm-repayment" data-id="${item.id}">Подтвердить получение</button>
      <button class="danger-button" type="button" data-action="prompt-reject-repayment" data-id="${item.id}">Отклонить</button>
      <button class="secondary-button" type="button" data-action="close-sheet">Решить позже</button>
    </div>`);
  haptic("medium");
  return true;
}

async function reloadBootstrap() {
  state.bootstrap = await api("/api/bootstrap");
  state.syncVersion = state.bootstrap.sync_version;
  state.details.clear();
  state.balanceData = null;
  state.globalHistory = null;
  const initials = state.bootstrap.user.full_name.split(/\s+/).map((part) => part[0]).join("").slice(0, 2);
  avatar.textContent = initials || "S";
  const pendingCount = Number(state.bootstrap.pending_repayment_count || 0);
  historyBadge.textContent = pendingCount > 99 ? "99+" : String(pendingCount);
  historyBadge.hidden = pendingCount === 0;
}

function empty(icon, text) {
  return `<div class="empty"><span class="empty-icon">${icon}</span>${e(text)}</div>`;
}

function collectionCards(rows, swipeAction = null) {
  if (!rows.length) return empty("🌿", "Сборов пока нет. Создайте первый с группой или без неё.");
  return `<div class="card-list">${rows.map((item) => {
    const canSwipe = Boolean(swipeAction) && item.admin_id === state.bootstrap.user.id;
    const card = `<button class="card collection-card swipe-card" type="button" data-action="${item.is_participant === false ? "preview-collection" : "open-collection"}" data-id="${item.id}">
      <span class="collection-icon">${item.status === "archived" ? "📦" : "🧾"}</span>
      <span>
        <span class="card-title">${e(item.title)}${item.admin_id === state.bootstrap.user.id ? '<span class="owner-mark">Ваш сбор</span>' : ""}</span>
        <span class="card-subtitle">${e(item.currency)} · ${item.participants_count ?? "—"} участников${item.is_personal ? " · без группы" : ""}${item.status === "archived" ? " · архив" : item.is_participant === false ? " · можно участвовать" : ""}</span>
      </span>
      <span class="chevron">›</span>
    </button>`;
    const actionLabel = swipeAction === "delete" ? "Удалить" : "В архив";
    const actionName = swipeAction === "delete" ? "swipe-delete" : "swipe-archive";
    return canSwipe ? `<div class="collection-swipe-row" data-swipe-id="${item.id}"><button class="swipe-danger-action" type="button" data-action="${actionName}" data-id="${item.id}">${actionLabel}</button>${card}</div>` : card;
  }).join("")}</div>`;
}

async function renderCollections() {
  state.nav = "collections";
  state.collection = null;
  title.textContent = state.bootstrap.context_chat_id ? "Сборы группы" : "Сборы";
  tg?.BackButton?.hide();
  if (!state.balanceData) state.balanceData = await api("/api/balance");
  const oweTotals = debtTotals(state.balanceData.personal_debts, "debtor_id");
  const owedTotals = debtTotals(state.balanceData.personal_debts, "creditor_id");
  const myDebts = state.balanceData.personal_debts.filter((debt) => debt.debtor_id === state.bootstrap.user.id);
  const quickPayments = quickPaymentRows(myDebts);
  const emptySummary = money(0, state.bootstrap.user.preferred_currency);
  const owe = Object.keys(oweTotals).length ? moneyMap(oweTotals) : emptySummary;
  const owed = Object.keys(owedTotals).length ? moneyMap(owedTotals) : emptySummary;
  const active = state.bootstrap.collections.filter((item) => item.status === "active");
  const archived = state.bootstrap.collections.filter((item) => item.status === "archived");
  app.innerHTML = `
    <div class="collection-summaries">
      <button class="summary-tile summary-owe" type="button" data-action="collections-summary"><small>Сколько я должен(а)</small><b>${owe}</b></button>
      <button class="summary-tile summary-owed" type="button" data-action="collections-summary"><small>Сколько мне должны</small><b>${owed}</b></button>
    </div>
    ${quickPayments ? `<section class="quick-pay-block ${state.quickPayExpanded ? "expanded" : ""}"><button class="quick-pay-toggle" type="button" data-action="toggle-quick-pay"><span class="quick-pay-symbol">↗</span><span><b>Быстрая оплата</b><small>${myDebts.length} ${myDebts.length === 1 ? "долг" : "долга"} · нажмите, чтобы ${state.quickPayExpanded ? "свернуть" : "развернуть"}</small></span><strong>${myDebts.length}</strong><i>${state.quickPayExpanded ? "⌃" : "⌄"}</i></button>${state.quickPayExpanded ? quickPayments : ""}</section>` : ""}
    <div class="section-head"><h2>Текущие</h2><button class="text-button" type="button" data-action="create">+ Новый</button></div>
    ${collectionCards(active, "archive")}
    ${archived.length ? `<div class="section-head"><h2>Архив</h2></div>${collectionCards(archived, "delete")}` : ""}`;
  updateNav();
}

async function renderBalance() {
  state.nav = "balance";
  state.collection = null;
  title.textContent = "Мой баланс";
  tg?.BackButton?.hide();
  app.innerHTML = `<section class="loading-card"><div class="spinner"></div><p>Считаем ваш баланс…</p></section>`;
  updateNav();
  const overview = state.balanceData || await api("/api/balance");
  state.balanceData = { ...overview, exchange: state.balanceData?.exchange || null };
  paintBalance();
  try {
    const exchange = await api("/api/rates");
    state.balanceData = { ...overview, exchange };
    if (state.nav === "balance" && !state.collection) paintBalance();
  } catch (error) {
    toast("Курсы валют временно недоступны — показываю исходные суммы", true);
  }
}

function paintBalance() {
  const data = state.balanceData;
  if (!data) return;
  const totals = new Map();
  const cards = data.collections.map((item) => {
    const collection = item.collection;
    const mine = item.amount;
    const status = balanceStatus(mine);
    totals.set(collection.currency, (totals.get(collection.currency) || 0) + mine);
    return `
      <button class="card collection-card" type="button" data-action="open-collection" data-id="${collection.id}">
        <span class="collection-icon status-icon" aria-hidden="true">${status.icon}</span>
        <span><span class="card-title">${e(collection.title)}</span><span class="card-subtitle">${status.label}</span></span>
        <span class="amount ${mine > 0 ? "positive" : mine < 0 ? "negative" : ""}">${money(Math.abs(mine), collection.currency)}</span>
      </button>`;
  });
  const preferred = state.bootstrap.user.preferred_currency;
  const convertedTotal = data.exchange ? Math.round([...totals.entries()].reduce((sum, [currency, amount]) => sum + amount * data.exchange.rates[currency] / data.exchange.rates[preferred], 0)) : null;
  const originalLabel = [...totals.entries()].filter(([, amount]) => amount).map(([currency, amount]) => `${amount >= 0 ? "+" : "−"}${money(Math.abs(amount), currency)}`).join(" · ") || "0";
  const netLabel = convertedTotal === null ? originalLabel : `${convertedTotal >= 0 ? "+" : "−"}${money(Math.abs(convertedTotal), preferred)}`;
  const personal = data.personal_debts.map((debt) => {
    const iOwe = debt.debtor_id === state.bootstrap.user.id;
    const personId = iOwe ? debt.creditor_id : debt.debtor_id;
    const personName = iOwe ? debt.creditor_name : debt.debtor_name;
    const personUsername = iOwe ? debt.creditor_username : debt.debtor_username;
    const initials = String(personName || "?").split(/\s+/).map((part) => part[0]).join("").slice(0, 2);
    return `<article class="personal-settlement ${iOwe ? "outgoing" : "incoming"}">
      <span class="person-avatar">${e(initials)}</span>
      <span class="personal-settlement-main"><small>${iOwe ? "Вы должны" : "Вам должен(а)"}</small><b>${userLink(personId, personName, personUsername)}</b><button class="collection-link" type="button" data-action="open-collection" data-id="${debt.collection_id}">${e(debt.collection_title)}</button></span>
      <span class="personal-settlement-side"><strong class="${iOwe ? "negative" : "positive"}">${money(debt.amount, debt.currency)}</strong>${iOwe ? `<button class="pay-small" type="button" data-action="quick-repay" data-collection-id="${debt.collection_id}" data-creditor-id="${debt.creditor_id}">Оплатить</button>` : ""}</span>
    </article>`;
  }).join("");
  app.innerHTML = `
    <section class="hero">
      <div class="hero-label">ОБЩИЙ БАЛАНС · ${e(preferred)}</div>
      <div class="hero-value">${netLabel}</div>
      <div class="hero-meta">${data.exchange ? `≈ ${data.exchange.stale ? "по последнему доступному" : "по официальному"} курсу НБРБ · ${e(originalLabel)}` : "По активным сборам · без конвертации"}</div>
    </section>
    <div class="segmented"><button type="button" data-action="balance-mode" data-mode="collections" class="${state.balanceMode === "collections" ? "active" : ""}">По сборам</button><button type="button" data-action="balance-mode" data-mode="personal" class="${state.balanceMode === "personal" ? "active" : ""}">Персональный</button></div>
    ${state.balanceMode === "collections"
    ? (cards.length ? `<div class="card-list">${cards.join("")}</div>` : empty("✅", "Активных долгов нет"))
    : (personal || empty("🤝", "Личных расчётов сейчас нет"))}`;
}

function renderProfile() {
  state.nav = "profile";
  state.collection = null;
  title.textContent = "Профиль";
  tg?.BackButton?.hide();
  const user = state.bootstrap.user;
  const methods = user.payment_methods || [];
  const notificationLabels = {
    notify_expenses: ["Затраты", "Новые и изменённые затраты"],
    notify_repayments: ["Возвраты долгов", "Запросы подтверждения и результат"],
    notify_collection_events: ["События сборов", "Участники, архив и управление"],
    notify_reminders: ["Напоминания", "Вежливые запросы рассчитаться"],
  };
  app.innerHTML = `
    <section class="card member-row">
      <div class="row-between"><div><div class="row-title">${userLink(user.id, user.full_name, user.username)}</div><div class="row-note">${user.username ? `@${e(user.username)}` : `Telegram ID ${user.id}`}</div></div><button class="text-button" type="button" data-action="edit-name">Изменить имя</button></div>
    </section>
    <div class="section-head"><h2>Общая валюта баланса</h2></div>
    <section class="card member-row">
      <div class="row-note">Используется только для общего итога. Суммы внутри сборов не меняются.</div>
      <label class="field compact-field"><span>Показывать общий баланс в</span><select id="preferred-currency">${state.bootstrap.currencies.map((currency) => `<option ${currency === user.preferred_currency ? "selected" : ""}>${currency}</option>`).join("")}</select></label>
    </section>
    <div class="section-head"><h2>Платежные данные · ${methods.length}</h2><button class="text-button" type="button" data-action="payment">${methods.length ? "Изменить" : "+ Добавить"}</button></div>
    <section class="card member-row">
      <div class="row-note">Реквизиты доступны другим пользователям только при оформлении возврата долга.</div>
      ${methods.length ? methods.map((method) => `<div class="profile-payment"><b>${e(method.bank_name || "Способ оплаты")}</b><span>${e(method.details)}</span></div>`).join("") : '<div class="row-title">Пока не добавлены</div>'}
    </section>
    <div class="section-head"><h2>Уведомления</h2></div>
    <section class="card notification-preferences">${Object.entries(notificationLabels).map(([key, labels]) => `<label class="preference-row"><span><b>${labels[0]}</b><small>${labels[1]}</small></span><input class="switch-input" type="checkbox" data-notification-pref="${key}" ${user.notification_preferences?.[key] ? "checked" : ""}></label>`).join("")}</section>
    <div class="status-banner">🔒 Вход подтверждается Telegram. Пароли и отдельная регистрация не нужны.</div>`;
  updateNav();
}

function quickPaymentRows(debts) {
  const mine = (debts || []).filter((debt) => debt.debtor_id === state.bootstrap.user.id);
  if (!mine.length) return "";
  return `<div class="quick-payments">${mine.map((debt) => `<article class="quick-payment"><span class="quick-payment-avatar">${e(String(debt.creditor_name || "?").split(/\s+/).map((part) => part[0]).join("").slice(0, 2))}</span><span class="quick-payment-info"><small>Перевести</small><b>${userLink(debt.creditor_id, debt.creditor_name, debt.creditor_username)}</b><em>${e(debt.collection_title || "")}</em></span><span class="quick-payment-side"><strong>${money(debt.amount, debt.currency)}</strong><button type="button" data-action="quick-repay" data-collection-id="${debt.collection_id}" data-creditor-id="${debt.creditor_id}">Оплатить</button></span></article>`).join("")}</div>`;
}

async function renderHistory(loadKind = null) {
  state.nav = "history";
  state.collection = null;
  title.textContent = "История";
  tg?.BackButton?.hide();
  if (!loadKind) app.innerHTML = `<section class="loading-card"><div class="spinner"></div><p>Собираем всю историю…</p></section>`;
  updateNav();
  const current = state.globalHistory;
  const transactionOffset = loadKind === "transactions" ? current.transactions.length : 0;
  const eventOffset = loadKind === "events" ? current.events.length : 0;
  let page;
  page = await api(`/api/history?transaction_offset=${transactionOffset}&event_offset=${eventOffset}`);
  if (!loadKind) {
    state.globalHistory = page;
  } else if (loadKind === "transactions") {
    current.transactions.push(...page.transactions);
    current.transaction_has_more = page.transaction_has_more;
    current.expense_stats = page.expense_stats;
  } else {
    current.events.push(...page.events);
    current.event_has_more = page.event_has_more;
    current.expense_stats = page.expense_stats;
  }
  const data = state.globalHistory;
  const eventLabels = {
    created: "создал(а) сбор", joined: "вступил(а) в сбор", left: "вышел(ла) из сбора",
    member_removed: "удалил(а) участника", admin_transferred: "передал(а) роль администратора",
    archived: "завершил(а) сбор", restored: "восстановил(а) сбор",
    funds_requested: "вежливо запросил(а) завершить расчёт",
  };
  const transactions = data.transactions.map((item) => {
    const isConfirmedRepayment = item.kind === "repayment" && item.confirmation_status === "confirmed";
    const isRejectedRepayment = item.kind === "repayment" && item.status === "cancelled" && item.cancelled_by === item.counterparty_id;
    const canConfirm = item.is_participant && item.collection_status === "active" && item.kind === "repayment" && item.status === "active" && item.confirmation_status === "pending" && item.counterparty_id === state.bootstrap.user.id;
    const canEdit = !canConfirm && !item.has_inactive_participants && item.is_participant && item.status === "active" && !isConfirmedRepayment && item.collection_status === "active" && (item.creator_id === state.bootstrap.user.id || item.collection_admin_id === state.bootstrap.user.id);
    const canCancel = !canConfirm && !item.has_inactive_participants && item.is_participant && item.status === "active" && item.collection_status === "active" && (item.collection_admin_id === state.bootstrap.user.id || (item.creator_id === state.bootstrap.user.id && !isConfirmedRepayment));
    const status = item.status === "cancelled" ? `<span class="pill cancelled">${isRejectedRepayment ? "отклонено" : "отменено"}</span>` : item.kind === "repayment" && item.confirmation_status === "pending" ? '<span class="pill pending">ожидает</span>' : item.kind === "repayment" ? '<span class="pill">подтверждено</span>' : "";
    const shares = item.kind === "expense" && item.shares.length ? `<div class="share-breakdown">${item.shares.map((share) => `<div class="row-note">${userLink(share.user_id, share.full_name, share.username)} — ${money(share.amount, item.currency)}</div>`).join("")}</div>` : "";
    const collectionTitle = item.is_participant ? `<button class="collection-link" type="button" data-action="open-collection" data-id="${item.collection_id}">${e(item.collection_title)}</button>` : `<span>${e(item.collection_title)}</span>`;
    const actions = canConfirm || canEdit || canCancel ? `<div class="transaction-actions">${canConfirm ? `<button class="mini-button confirm" type="button" data-action="confirm-repayment" data-id="${item.id}" data-return="global">✅ Подтвердить получение</button><button class="mini-button danger" type="button" data-action="reject-repayment" data-id="${item.id}" data-return="global">❌ Отклонить</button>` : ""}${canEdit ? `<button class="mini-button" type="button" data-action="edit-history-transaction" data-id="${item.id}" data-collection-id="${item.collection_id}">Изменить</button>` : ""}${canCancel ? `<button class="mini-button danger" type="button" data-action="cancel-transaction" data-id="${item.id}" data-return="global">Удалить</button>` : ""}</div>` : "";
    const description = item.comment ? e(item.comment) : item.kind === "expense" ? "Затрата" : `Возврат → ${userLink(item.counterparty_id, item.counterparty_name, item.counterparty_username)}`;
    return `<article class="history-row"><div class="row-between"><div><div class="row-title history-collection-title"><span>${item.kind === "expense" ? "💸" : "🤝"}</span>${collectionTitle}</div><div class="row-note">${userLink(item.creator_id, item.creator_name, item.creator_username)} · ${shortDate(item.created_at)}</div></div><div class="amount">${money(item.amount, item.currency)}</div></div><div class="row-note">${description} ${status}</div>${shares}${actions}</article>`;
  }).join("");
  const events = data.events.map((item) => `<article class="history-row"><div class="row-title">${item.is_participant ? `<button class="collection-link" type="button" data-action="open-collection" data-id="${item.collection_id}">${e(item.collection_title)}</button>` : e(item.collection_title)}</div><div class="row-note">${shortDate(item.created_at)} · ${userLink(item.actor_id, item.actor_name, item.actor_username)} ${e(eventLabels[item.kind] || item.kind)}${item.target_name && item.target_name !== item.actor_name ? ` · ${userLink(item.target_user_id, item.target_name, item.target_username)}` : ""}</div></article>`).join("");
  app.innerHTML = `<button class="hero history-stats-trigger" type="button" data-action="expense-statistics"><span><span class="hero-label">МОИ РАСХОДЫ С НАЧАЛА МЕСЯЦА</span><span class="hero-value">${moneyMap(data.expense_stats.monthly_personal_by_currency)}</span><span class="hero-meta">На меня распределено · возвращено ${moneyMap(data.expense_stats.monthly_repaid_by_currency)}</span></span><i>›</i></button><div class="section-head"><h2>Транзакции</h2></div>${transactions ? `<div class="card">${transactions}</div>` : empty("📜", "Транзакций пока нет")}${data.transaction_has_more ? '<button class="load-more" type="button" data-action="load-history" data-kind="transactions">Загрузить ещё</button>' : ""}<div class="section-head"><h2>История сборов</h2></div>${events ? `<div class="card">${events}</div>` : empty("🧾", "Событий пока нет")}${data.event_has_more ? '<button class="load-more" type="button" data-action="load-history" data-kind="events">Загрузить ещё</button>' : ""}`;
}

function expenseStatisticsSheet() {
  const stats = state.globalHistory?.expense_stats;
  if (!stats) return;
  const collections = stats.by_collection.map((item) => `<div class="statistics-collection"><div class="row-between"><div><div class="row-title">${e(item.title)}</div><div class="row-note">${item.operation_count} операций</div></div><div class="amount">${money(item.personal_amount, item.currency)}</div></div><div class="statistics-flow"><span>Оплачено вами <b>${money(item.paid_amount, item.currency)}</b></span><span>Возвращено вами <b>${money(item.repaid_amount, item.currency)}</b></span>${item.received_amount ? `<span>Получено возвратов <b>${money(item.received_amount, item.currency)}</b></span>` : ""}</div></div>`).join("");
  showSheet(`<h2>Мои расходы</h2><p class="sheet-intro">«На меня распределено» показывает вашу личную долю общих затрат. Возвраты учитываются отдельно как фактически переведённые вами средства.</p><div class="statistics-period"><span>Этот месяц</span><strong>${moneyMap(stats.monthly_personal_by_currency)}</strong><small>${stats.monthly_personal_count} затрат относятся на вас</small></div><div class="stats-grid statistics-grid"><div><small>Оплачено вами</small><b>${moneyMap(stats.monthly_paid_by_currency)}</b><span>${stats.monthly_paid_count} операций</span></div><div><small>Возвращено вами</small><b>${moneyMap(stats.monthly_repaid_by_currency)}</b><span>${stats.monthly_repaid_count} переводов</span></div><div><small>Получено возвратов</small><b>${moneyMap(stats.monthly_received_by_currency)}</b><span>${stats.monthly_received_count} переводов</span></div><div><small>Ваша доля за всё время</small><b>${moneyMap(stats.total_personal_by_currency)}</b><span>${stats.personal_count} затрат</span></div></div><div class="section-head"><h2>По сборам</h2></div><div class="statistics-collections">${collections || '<div class="card debt-row">Данных пока нет</div>'}</div><div class="sheet-actions"><button class="secondary-button" type="button" data-action="close-sheet">Закрыть</button></div>`);
}

function renderInvitation() {
  const invitation = state.bootstrap.invitation;
  state.nav = "collections";
  title.textContent = invitation.collection.title;
  updateNav();
  app.innerHTML = `<section class="hero"><div class="hero-label">ПРИГЛАШЕНИЕ В СБОР</div><div class="hero-value">${e(invitation.collection.title)}</div><div class="hero-meta">Валюта · ${e(invitation.collection.currency)}</div></section><div class="status-banner">Telegram безопасно передаст ваш ID. Разрешите уведомления, чтобы узнавать о тратах и возвратах, даже если вас нет в группе сбора.</div><div class="sheet-actions"><button class="primary-button" type="button" data-action="join-subscribe" data-id="${invitation.collection.id}">🔔 Участвовать и получать уведомления</button><button class="secondary-button" type="button" data-action="join-invitation" data-id="${invitation.collection.id}">Участвовать без уведомлений</button></div>`;
}

function renderWelcome() {
  const firstName = state.bootstrap.user.full_name.split(/\s+/)[0];
  title.textContent = "Добро пожаловать";
  nav.hidden = true;
  app.innerHTML = `
    <section class="welcome-card">
      <div class="welcome-mark">S</div>
      <div class="hero-label">SHAKEONIT</div>
      <h2>${e(firstName)}, общие расходы — без неловких подсчётов</h2>
      <p>Telegram уже подтвердил ваш профиль. Никаких логинов, паролей и отдельной регистрации.</p>
      <div class="welcome-points">
        <div><span>🧾</span><b>Все сборы рядом</b><small>Расходы, долги и история в одном месте</small></div>
        <div><span>⚡</span><b>Операции за секунды</b><small>Добавляйте траты и сразу видьте результат</small></div>
        <div><span>🔒</span><b>Только Telegram</b><small>Privacy Mode остается включённым</small></div>
      </div>
      <button class="primary-button" type="button" data-action="welcome-continue">Начать</button>
    </section>`;
}

async function continueAfterWelcome() {
  nav.hidden = false;
  if (state.bootstrap.invitation?.is_participant) {
    await openCollection(state.bootstrap.invitation.collection.id);
    showPendingRepaymentConfirmation();
    return;
  }
  if (state.bootstrap.invitation) return renderInvitation();
  if (state.launchIntent === "create") {
    state.launchIntent = null;
    renderCollections();
    return createSheet();
  }
  await renderCollections();
  showPendingRepaymentConfirmation();
}

function updateNav() {
  nav.querySelectorAll("button").forEach((button) => button.classList.toggle("active", button.dataset.nav === state.nav));
}

async function loadDetails(id, force = false) {
  if (!force && state.details.has(Number(id))) return state.details.get(Number(id));
  const data = await api(`/api/collections/${id}?history_limit=${state.collectionHistoryLimit}&events_limit=${state.collectionEventsLimit}`);
  state.details.set(Number(id), data);
  return data;
}

async function openCollection(id, tab = "overview", force = false) {
  if (state.collection?.collection?.id !== Number(id)) {
    state.collectionHistoryLimit = 20;
    state.collectionEventsLimit = 20;
  }
  if (!state.collection) state.collectionReturn = state.nav;
  state.collectionTab = tab;
  app.innerHTML = `<section class="loading-card"><div class="spinner"></div><p>Обновляем сбор…</p></section>`;
  const data = await loadDetails(id, force);
  state.collection = data;
  title.textContent = data.collection.title;
  tg?.BackButton?.show();
  renderCollection();
}

function renderCollection() {
  const data = state.collection;
  const collection = data.collection;
  const me = state.bootstrap.user.id;
  const isAdmin = collection.admin_id === me;
  const myBalance = data.balances.find((item) => item.user_id === me)?.amount || 0;
  const myDebtors = data.debts.filter((debt) => debt.creditor_id === me);
  const settlementStatus = myBalance > 0
    ? `💰 Вам должны ${money(myBalance, collection.currency)}`
    : myBalance < 0
      ? `💸 Вы должны ${money(-myBalance, collection.currency)}`
      : "🤝 Ваш расчёт закрыт";
  const activeParticipants = data.participants.filter((member) => member.active !== false);
  const tabs = [
    ["overview", "Обзор"], ["history", "История"], ["members", "Люди"],
    ...(isAdmin ? [["admin", "Управление"]] : []),
  ];
  app.innerHTML = `
    <section class="hero">
      <div class="hero-top"><div class="hero-label">ВСЕГО ЗАТРАТ · ${e(collection.currency)}</div><div class="hero-status">${settlementStatus}</div></div>
      <div class="hero-value">${money(data.total, collection.currency)}</div>
      <div class="hero-meta">${activeParticipants.length} участников · ${collection.is_personal ? "уведомления через бота" : "Telegram-группа"} · ${collection.status === "active" ? "активен" : "в архиве"}</div>
    </section>
    ${collection.status === "active" ? `<div class="quick-actions"><button class="action-button" type="button" data-action="expense">💸 Добавить затрату</button><button class="action-button" type="button" data-action="repay">🤝 Вернуть долг</button></div>` : `<div class="status-banner">📦 Сбор находится в архиве. Балансы и история доступны без изменений.</div>`}
    ${collection.status === "active" ? `<button class="request-funds" type="button" data-action="request-funds" ${myDebtors.length ? "" : "disabled"}><span>🔔</span><span><b>Запросить средства</b><small>${myDebtors.length ? `Вежливо напомнить должникам · ${myDebtors.length}` : "Сейчас вам никто не должен(а)"}</small></span><i>›</i></button>` : ""}
    ${collection.status === "active" ? `<button class="notification-toggle ${data.notifications_enabled ? "enabled" : ""}" type="button" data-action="toggle-notifications" data-enabled="${data.notifications_enabled}"><span>${data.notifications_enabled ? "🔔" : "🔕"}</span><span><b>${data.notifications_enabled ? "Уведомления включены" : "Получать уведомления"}</b><small>${data.notifications_enabled ? "Бот сообщит о новых операциях в личном чате" : "Даже если вас нет в Telegram-группе"}</small></span><i>›</i></button>` : ""}
    <div class="tabs">${tabs.map(([key, label]) => `<button type="button" data-action="tab" data-tab="${key}" class="${state.collectionTab === key ? "active" : ""}">${label}</button>`).join("")}</div>
    <section class="panel">${renderCollectionPanel(data, isAdmin)}</section>`;
}

function renderCollectionPanel(data, isAdmin) {
  const collection = data.collection;
  if (state.collectionTab === "overview") {
    const balances = data.participants.map((member) => {
      const amount = data.balances.find((item) => item.user_id === member.id)?.amount || 0;
      return `<div class="balance-row"><div class="row-between"><div><div class="row-title">${userLink(member.id, member.full_name, member.username)}${member.id === state.bootstrap.user.id ? " · вы" : ""}${member.active === false ? " · вышел(ла)" : ""}</div><div class="row-note">${amount > 0 ? "должны участники" : amount < 0 ? "должен(а) участникам" : "расчёт закрыт"}</div></div><div class="amount ${amount > 0 ? "positive" : amount < 0 ? "negative" : ""}">${amount > 0 ? "+" : amount < 0 ? "−" : ""}${money(Math.abs(amount), collection.currency)}</div></div></div>`;
    }).join("");
    const debts = data.debts.map((debt) => `<div class="debt-row"><div class="row-title">${userLink(debt.debtor_id, debt.debtor_name, debt.debtor_username)} → ${userLink(debt.creditor_id, debt.creditor_name, debt.creditor_username)}</div><div class="row-note">Перевести ${money(debt.amount, collection.currency)}</div></div>`).join("");
    return `<div class="section-head"><h2>Балансы</h2></div><div class="card">${balances}</div><div class="section-head"><h2>Кто кому</h2></div><div class="card">${debts || `<div class="debt-row">✅ Никто никому не должен(а)</div>`}</div>`;
  }
  if (state.collectionTab === "history") {
    const transactions = data.history.map((item) => {
      const isConfirmedRepayment = item.kind === "repayment" && item.confirmation_status === "confirmed";
      const isRejectedRepayment = item.kind === "repayment" && item.status === "cancelled" && item.cancelled_by === item.counterparty_id;
      const canConfirm = item.kind === "repayment" && item.status === "active" && item.confirmation_status === "pending" && item.counterparty_id === state.bootstrap.user.id;
      const canEdit = !canConfirm && !item.has_inactive_participants && item.status === "active" && !isConfirmedRepayment && collection.status === "active" && (isAdmin || item.creator_id === state.bootstrap.user.id);
      const canCancel = !canConfirm && !item.has_inactive_participants && item.status === "active" && collection.status === "active" && (isAdmin || (item.creator_id === state.bootstrap.user.id && !isConfirmedRepayment));
      const subject = item.kind === "expense" ? e(item.comment || "Затрата") : `Возврат → ${userLink(item.counterparty_id, item.counterparty_name, item.counterparty_username)}`;
      const status = item.status === "cancelled" ? `<span class="pill cancelled">${isRejectedRepayment ? "отклонено" : "отменено"}</span>` : item.kind === "repayment" && item.confirmation_status === "pending" ? '<span class="pill pending">ожидает подтверждения</span>' : item.kind === "repayment" ? '<span class="pill">подтверждено</span>' : "";
      const shares = item.kind === "expense" ? `<div class="share-breakdown">${item.shares.map((share) => `<div class="row-note">${userLink(share.user_id, share.full_name, share.username)} — ${money(share.amount, collection.currency)}</div>`).join("")}</div>` : "";
      return `<article class="history-row"><div class="row-between"><div><div class="row-title">${item.kind === "expense" ? "💸" : "🤝"} ${subject}</div><div class="row-note">${userLink(item.creator_id, item.creator_name, item.creator_username)} · ${shortDate(item.created_at)}</div></div><div class="amount">${money(item.amount, collection.currency)}</div></div><div class="row-note">${item.kind === "expense" ? "Распределение по людям" : "Фактический перевод"} ${status}</div>${shares}${canConfirm ? `<div class="transaction-actions"><button class="mini-button confirm" type="button" data-action="confirm-repayment" data-id="${item.id}">✅ Подтвердить получение</button><button class="mini-button danger" type="button" data-action="reject-repayment" data-id="${item.id}">❌ Отклонить</button></div>` : ""}${canEdit || canCancel ? `<div class="transaction-actions">${canEdit ? `<button class="mini-button" type="button" data-action="edit-transaction" data-id="${item.id}">Изменить</button>` : ""}${canCancel ? `<button class="mini-button danger" type="button" data-action="cancel-transaction" data-id="${item.id}">Отменить</button>` : ""}</div>` : ""}</article>`;
    }).join("");
    const eventLabels = {
      created: "создал(а) сбор", joined: "вступил(а) в сбор", left: "вышел(ла) из сбора",
      member_removed: "удалил(а) участника", admin_transferred: "передал(а) роль администратора",
      archived: "завершил(а) сбор", restored: "восстановил(а) сбор",
      funds_requested: "вежливо запросил(а) завершить расчёт",
    };
    const events = data.events.map((item) => `<article class="history-row"><div class="row-title">${shortDate(item.created_at)} · ${userLink(item.actor_id, item.actor_name, item.actor_username)} ${e(eventLabels[item.kind] || item.kind)}</div>${item.target_name && item.target_name !== item.actor_name ? `<div class="row-note">Участник: ${userLink(item.target_user_id, item.target_name, item.target_username)}</div>` : ""}</article>`).join("");
    if (!transactions && !events) return empty("📜", "История пока пуста");
    return `<div class="section-head"><h2>Транзакции</h2></div>${transactions ? `<div class="card">${transactions}</div>` : empty("📜", "Транзакций пока нет")}${data.history_has_more ? '<button class="load-more" type="button" data-action="load-collection-history" data-kind="transactions">Загрузить ещё</button>' : ""}<div class="section-head"><h2>Участники и сбор</h2></div>${events ? `<div class="card">${events}</div>` : empty("👥", "Событий пока нет")}${data.events_has_more ? '<button class="load-more" type="button" data-action="load-collection-history" data-kind="events">Загрузить ещё</button>' : ""}`;
  }
  if (state.collectionTab === "members") {
    const invite = collection.status === "active" ? `<button class="invite-button" type="button" data-action="share-invite">👥<span><b>Пригласить друзей</b><small>Выбрать человека или Telegram-группу</small></span><i>›</i></button>` : "";
    const activeParticipants = data.participants.filter((member) => member.active !== false);
    const members = activeParticipants.map((member) => `<div class="member-row"><div class="row-between"><div><div class="row-title">${userLink(member.id, member.full_name, member.username)} ${member.is_admin ? "👑" : ""}</div><div class="row-note">${member.username ? `@${e(member.username)}` : `ID ${member.id}`}</div></div></div></div>`).join("");
    return `${invite}<div class="section-head"><h2>Участники · ${activeParticipants.length}</h2></div><div class="card">${members}</div>${collection.status === "active" && !isAdmin ? '<div class="sheet-actions"><button class="danger-button" type="button" data-action="leave">Выйти из сбора</button></div>' : ""}`;
  }
  return `<div class="card member-row"><div class="row-title">Администратор сбора</div><div class="row-note">Передача роли и удаление участников доступны только при соблюдении балансов.</div></div><div class="sheet-actions">${collection.status === "active" ? '<button class="secondary-button" type="button" data-action="transfer">Передать администратора</button><button class="secondary-button" type="button" data-action="remove-member">Удалить участника</button><button class="danger-button" type="button" data-action="archive">Завершить и архивировать</button>' : '<button class="primary-button" type="button" data-action="restore">Восстановить сбор</button><button class="danger-button" type="button" data-action="delete-collection">Удалить сбор навсегда</button>'}</div>`;
}

async function shareCollection(collection) {
  haptic();
  if (tg?.shareMessage) {
    const prepared = await api(`/api/collections/${collection.id}/prepare-share`, {
      method: "POST",
      body: "{}",
    });
    return await new Promise((resolve) => {
      tg.shareMessage(prepared.prepared_message_id, (shared) => {
        if (shared) toast("Приглашение отправлено");
        resolve(Boolean(shared));
      });
    });
  }
  toast("Обновите Telegram, чтобы отправлять приглашения без ссылок", true);
  return false;
}

function createdCollectionInviteSheet(collection) {
  showSheet(`<div class="success-mark">✓</div><h2>Сбор готов</h2><p class="sheet-intro">Пригласите сразу несколько друзей или групп. Позже приглашение всегда будет доступно во вкладке «Люди» внутри сбора.</p><div class="created-collection"><b>${e(collection.title)}</b><span>${e(collection.currency)}</span></div><div class="sheet-actions"><button class="primary-button" type="button" data-action="share-created">👥 Выбрать людей и группы</button><button class="secondary-button" type="button" data-action="close-sheet">Продолжить без приглашения</button></div>`);
}

function createSheet() {
  const chatId = state.bootstrap.context_chat_id || 0;
  const intro = chatId
    ? "Сбор будет связан с текущей Telegram-группой, и приглашение появится в её чате."
    : "Сбор будет работать без Telegram-группы — уведомления придут лично от бота.";
  showSheet(`<h2>Новый сбор</h2><p class="sheet-intro">${intro}</p><form id="create-form"><input type="hidden" name="chat_id" value="${chatId}"><label class="field"><span>Название</span><input name="title" minlength="2" maxlength="80" placeholder="Например, Поездка в Варшаву" required></label><label class="field"><span>Валюта</span><select name="currency">${state.bootstrap.currencies.map((currency) => `<option>${currency}</option>`).join("")}</select></label><div class="sheet-actions"><button class="primary-button" type="submit">Создать сбор</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

function expenseSheet() {
  const data = state.collection;
  const activeParticipants = data.participants.filter((member) => member.active !== false);
  showSheet(`<h2>Добавить затрату</h2><p class="sheet-intro">Сумма будет поровну распределена между отмеченными людьми.</p><form id="expense-form"><label class="field"><span>Сумма · ${e(data.collection.currency)}</span><input name="amount" inputmode="decimal" placeholder="0,00" required></label><label class="field"><span>Комментарий</span><input name="comment" maxlength="200" placeholder="Например, билеты"></label><div class="row-between"><span class="row-title">На кого делим</span><button class="text-button" type="button" data-action="select-all">Выбрать всех</button></div><div class="check-list">${activeParticipants.map((member) => `<label class="check-row"><input type="checkbox" name="participant" value="${member.id}"><span>${userLink(member.id, member.full_name, member.username)}${member.id === state.bootstrap.user.id ? " · вы" : ""}</span></label>`).join("")}</div><div class="sheet-actions"><button class="primary-button" type="submit">Добавить затрату</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

function repaySheet(preferredCreditorId = null) {
  const data = state.collection;
  const debts = data.debts.filter((debt) => debt.debtor_id === state.bootstrap.user.id && debt.repayable_amount > 0);
  if (!debts.length) {
    toast("Свободного остатка нет: долг уже возвращён или ожидает подтверждения");
    return;
  }
  const selectedIndex = Math.max(0, debts.findIndex((debt) => debt.creditor_id === Number(preferredCreditorId)));
  const cards = debts.map((debt, index) => {
    const member = data.participants.find((item) => item.id === debt.creditor_id);
    const methods = member?.payment_methods?.length
      ? member.payment_methods
      : member?.payment_details ? [{ bank_name: member.bank_name, details: member.payment_details }] : [];
    const methodRows = methods.length ? methods.map((method) => `<div class="repay-method">${method.bank_name ? `<span class="row-note">Банк: ${e(method.bank_name)}</span>` : ""}<div class="payment-value-row"><span class="payment-value">${e(method.details)}</span><button class="copy-payment" type="button" data-action="copy-payment" data-value="${encodeURIComponent(method.details)}">Копировать</button></div></div>`).join("") : '<span class="row-note">Получатель пока не добавил(а) платежные данные</span>';
    return `<div class="payment-card" data-payment="${debt.creditor_id}" ${index === selectedIndex ? "" : "hidden"}><div class="payment-card-head"><b>💳 Данные для перевода · ${userLink(member?.id, member?.full_name, member?.username)}</b></div>${methodRows}</div>`;
  }).join("");
  showSheet(`<h2>Вернуть долг</h2><p class="sheet-intro">После записи получатель должен(а) подтвердить деньги. До этого баланс не изменится.</p><form id="repay-form"><label class="field"><span>Получатель</span><select name="creditor_id" data-action="repay-creditor">${debts.map((debt, index) => `<option value="${debt.creditor_id}" data-max="${debt.repayable_amount}" ${index === selectedIndex ? "selected" : ""}>${e(debt.creditor_name)} · доступно ${money(debt.repayable_amount, data.collection.currency)}</option>`).join("")}</select></label>${cards}<label class="field"><span>Переведено · ${e(data.collection.currency)}</span><span class="amount-input"><input name="amount" inputmode="decimal" placeholder="0,00" required><button type="button" data-action="repay-max">MAX</button></span></label><label class="field"><span>Комментарий</span><input name="comment" maxlength="200" placeholder="Необязательно"></label><div class="sheet-actions"><button class="primary-button" type="submit">Отправить на подтверждение</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

async function copyText(value) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const node = document.createElement("textarea");
  node.value = value;
  node.style.position = "fixed";
  node.style.opacity = "0";
  document.body.append(node);
  node.select();
  document.execCommand("copy");
  node.remove();
}

function editSheet(transactionId, collectionData = state.collection, returnTo = "collection") {
  const item = collectionData.history.find((row) => row.id === Number(transactionId));
  const selectedIds = new Set(item.shares.map((share) => share.user_id));
  const candidates = collectionData.participants
    .filter((member) => member.active !== false)
    .map((member) => ({ ...member, active: true }));
  item.shares.forEach((share) => {
    if (!candidates.some((member) => member.id === share.user_id)) {
      candidates.push({ id: share.user_id, username: share.username, full_name: share.full_name, active: false });
    }
  });
  const participantEditor = item.kind === "expense" ? `<div class="row-between"><span class="row-title">На кого делим</span><button class="text-button" type="button" data-action="select-all">Выбрать всех</button></div><div class="check-list">${candidates.map((member) => `<label class="check-row"><input type="checkbox" name="participant" value="${member.id}" ${selectedIds.has(member.id) ? "checked" : ""}><span>${userLink(member.id, member.full_name, member.username)}${member.id === state.bootstrap.user.id ? " · вы" : ""}${member.active ? "" : " · вышел(ла) из сбора"}</span></label>`).join("")}</div>` : "";
  const intro = item.kind === "expense"
    ? "После сохранения сумма будет поровну распределена между отмеченными людьми, а балансы пересчитаются."
    : "Изменённый возврат останется на подтверждении у получателя.";
  showSheet(`<h2>Изменить транзакцию</h2><p class="sheet-intro">${intro}</p><form id="edit-form" data-id="${item.id}" data-return="${returnTo}"><label class="field"><span>Сумма · ${e(collectionData.collection.currency)}</span><input name="amount" inputmode="decimal" value="${(item.amount / 100).toFixed(2).replace(".", ",")}" required></label><label class="field"><span>Комментарий</span><input name="comment" maxlength="200" value="${e(item.comment)}"></label>${participantEditor}<div class="sheet-actions"><button class="primary-button" type="submit">Сохранить</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

function paymentSheet() {
  const methods = state.bootstrap.user.payment_methods || [];
  const rows = (methods.length ? methods : [{ bank_name: "", details: "" }]).map((method) => paymentMethodEditor(method)).join("");
  showSheet(`<h2>Платежные данные</h2><p class="sheet-intro">Добавьте несколько удобных способов. Номер карты и другие реквизиты видны только на экране возврата долга.</p><form id="payment-form"><div id="payment-method-editors">${rows}</div><button class="add-method-button" type="button" data-action="add-payment-method">+ Ещё способ оплаты</button><div class="sheet-actions"><button class="primary-button" type="submit">Сохранить</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

function paymentMethodEditor(method = {}) {
  return `<div class="payment-method-editor"><button type="button" class="remove-method" data-action="remove-payment-method" aria-label="Удалить">×</button><label class="field"><span>Банк</span><input name="method_bank" maxlength="100" placeholder="Например, Альфа-Банк" value="${e(method.bank_name || "")}"></label><label class="field"><span>Реквизиты</span><textarea name="method_details" maxlength="500" placeholder="Телефон СБП или номер карты">${e(method.details || "")}</textarea></label></div>`;
}

function nameSheet() {
  showSheet(`<h2>Ваше имя</h2><p class="sheet-intro">Так вас будут видеть участники сборов.</p><form id="name-form"><label class="field"><span>Имя</span><input name="full_name" minlength="2" maxlength="80" value="${e(state.bootstrap.user.full_name)}" required></label><div class="sheet-actions"><button class="primary-button" type="submit">Сохранить</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

function memberActionSheet(type) {
  const candidates = state.collection.participants.filter((member) => member.active !== false && member.id !== state.bootstrap.user.id);
  const titleText = type === "transfer" ? "Передать администратора" : "Удалить участника";
  showSheet(`<h2>${titleText}</h2><p class="sheet-intro">${type === "transfer" ? "Новый администратор получит все права управления сбором." : "Удалить можно участника с нулевым балансом."}</p><form id="member-action-form" data-type="${type}"><label class="field"><span>Участник</span><select name="user_id">${candidates.map((member) => `<option value="${member.id}">${e(member.full_name)}</option>`).join("")}</select></label><div class="sheet-actions"><button class="${type === "transfer" ? "primary-button" : "danger-button"}" type="submit">${titleText}</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

function confirmAction(message) {
  return new Promise((resolve) => {
    if (tg?.showConfirm) tg.showConfirm(message, resolve);
    else resolve(window.confirm(message));
  });
}

function requestWritePermission() {
  if (tg?.initDataUnsafe?.user?.allows_write_to_pm) return Promise.resolve(true);
  if (!tg?.requestWriteAccess) return Promise.resolve(false);
  return new Promise((resolve) => tg.requestWriteAccess((allowed) => resolve(Boolean(allowed))));
}

async function refreshCurrent(tab = state.collectionTab) {
  await reloadBootstrap();
  if (state.collection) await openCollection(state.collection.collection.id, tab, true);
}

async function refreshVisibleView() {
  const collectionId = state.collection?.collection?.id;
  const collectionTab = state.collectionTab;
  await reloadBootstrap();
  if (collectionId) await openCollection(collectionId, collectionTab, true);
  else if (state.nav === "history") await renderHistory();
  else if (state.nav === "balance") await renderBalance();
  else if (state.nav === "profile") renderProfile();
  else await renderCollections();
  showPendingRepaymentConfirmation();
}

async function checkForUpdates(force = false) {
  if (!state.bootstrap || state.refreshInFlight || state.busy || !sheetLayer.hidden) return;
  if (document.visibilityState !== "visible") return;
  const now = Date.now();
  if (!force && now - state.lastSyncCheck < 5000) return;
  state.lastSyncCheck = now;
  state.refreshInFlight = true;
  try {
    const result = await api("/api/sync");
    if (result.sync_version !== state.syncVersion) await refreshVisibleView();
  } catch (error) {
    console.debug("Background sync postponed", error);
  } finally {
    state.refreshInFlight = false;
  }
}

function rememberView(nextView) {
  if (!state.collection && state.nav !== nextView) {
    if (state.viewStack[state.viewStack.length - 1] !== state.nav) state.viewStack.push(state.nav);
    if (state.viewStack.length > 12) state.viewStack.shift();
  }
}

async function renderTopLevel(view) {
  if (view === "balance") return await renderBalance();
  if (view === "history") return await renderHistory();
  if (view === "profile") return renderProfile();
  return await renderCollections();
}

async function navigateBack(closeAtRoot = false) {
  if (!sheetLayer.hidden) {
    closeSheet();
    return true;
  }
  if (state.collection) {
    const returnTo = state.collectionReturn;
    state.collection = null;
    await renderTopLevel(returnTo);
    return true;
  }
  const previous = state.viewStack.pop();
  if (previous) {
    await renderTopLevel(previous);
    return true;
  }
  if (closeAtRoot) tg?.close();
  return false;
}

function scheduleSync() {
  clearTimeout(state.syncTimer);
  state.syncTimer = setTimeout(async () => {
    await checkForUpdates();
    scheduleSync();
  }, 20000);
}

app.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-action]");
  if (!target || state.busy) return;
  const action = target.dataset.action;
  const revealedSwipe = target.closest(".collection-swipe-row.revealed");
  if (revealedSwipe && action === "open-collection") {
    revealedSwipe.classList.remove("revealed");
    return;
  }
  try {
    if (action === "open-user") {
      openTelegramUser(target);
      return;
    }
    if (action === "collections-summary") {
      rememberView("balance");
      state.balanceMode = "personal";
      return await renderBalance();
    }
    if (action === "toggle-quick-pay") {
      state.quickPayExpanded = !state.quickPayExpanded;
      haptic();
      return await renderCollections();
    }
    if (action === "swipe-archive") {
      if (!await confirmAction("Завершить сбор и отправить его в архив на 30 дней?")) return;
      setBusy(target, true);
      const result = await api(`/api/collections/${target.dataset.id}/archive`, { method: "POST", body: "{}" });
      reportToast(result, "Сбор в архиве");
      await reloadBootstrap();
      return await renderCollections();
    }
    if (action === "swipe-delete") {
      if (!await confirmAction("Удалить архивный сбор навсегда? Все операции и история будут удалены без возможности восстановления.")) return;
      setBusy(target, true);
      const result = await api(`/api/collections/${target.dataset.id}`, { method: "DELETE" });
      reportToast(result, "Сбор удалён");
      await reloadBootstrap();
      return await renderCollections();
    }
    if (action === "quick-repay") {
      await openCollection(target.dataset.collectionId, "history", true);
      repaySheet(target.dataset.creditorId);
      return;
    }
    if (action === "balance-mode") {
      state.balanceMode = target.dataset.mode;
      paintBalance();
      return;
    }
    if (action === "load-history") return await renderHistory(target.dataset.kind);
    if (action === "expense-statistics") return expenseStatisticsSheet();
    if (action === "load-collection-history") {
      if (target.dataset.kind === "transactions") state.collectionHistoryLimit += 20;
      else state.collectionEventsLimit += 20;
      return await openCollection(state.collection.collection.id, "history", true);
    }
    if (action === "open-collection") return await openCollection(target.dataset.id);
    if (action === "preview-collection") {
      const collection = state.bootstrap.collections.find((item) => item.id === Number(target.dataset.id));
      state.bootstrap.invitation = { collection, is_participant: false };
      return renderInvitation();
    }
    if (action === "welcome-continue") return continueAfterWelcome();
    if (action === "join-invitation" || action === "join-subscribe") {
      setBusy(target, true);
      const requestedSubscription = action === "join-subscribe";
      const subscribe = requestedSubscription ? await requestWritePermission() : false;
      const result = await api(`/api/collections/${target.dataset.id}/join`, { method: "POST", body: JSON.stringify({ subscribe }) });
      if (result.already_participant) toast("Вы уже участвуете в этом сборе");
      else reportToast(result, result.notifications_enabled ? "Вы участвуете · уведомления включены" : "Вы участвуете в сборе");
      if (requestedSubscription && !result.notifications_enabled) toast("Вы участвуете, но Telegram не разрешил личные уведомления", true);
      await reloadBootstrap();
      return await openCollection(target.dataset.id, "overview", true);
    }
    if (action === "create") return createSheet();
    if (action === "expense") return expenseSheet();
    if (action === "repay") return repaySheet();
    if (action === "tab") { state.collectionTab = target.dataset.tab; renderCollection(); return; }
    if (action === "edit-transaction") return editSheet(target.dataset.id);
    if (action === "edit-history-transaction") {
      const previousLimit = state.collectionHistoryLimit;
      state.collectionHistoryLimit = 500;
      const details = await loadDetails(target.dataset.collectionId, true);
      state.collectionHistoryLimit = previousLimit;
      return editSheet(target.dataset.id, details, "global");
    }
    if (action === "request-funds") {
      if (!await confirmAction("Отправить всем вашим должникам вежливое напоминание о расчёте?")) return;
      setBusy(target, true);
      const result = await api(`/api/collections/${state.collection.collection.id}/request-funds`, { method: "POST", body: "{}" });
      if (result.notifications_queued) {
        toast(`Напоминания поставлены в отправку · ${result.debtors_count}`);
        return await refreshCurrent(state.collectionTab);
      }
      if (result.failed_count) {
        toast(`Отправлено ${result.notifications_sent} из ${result.debtors_count}. Остальные ещё не подключили бота`, true);
      } else {
        toast(`Напоминание отправлено · ${result.notifications_sent}`);
      }
      return await refreshCurrent(state.collectionTab);
    }
    if (action === "payment") return paymentSheet();
    if (action === "edit-name") return nameSheet();
    if (action === "toggle-notifications") {
      const enabled = target.dataset.enabled === "true";
      if (!enabled && !await requestWritePermission()) {
        toast("Разрешите боту присылать сообщения в окне Telegram", true);
        return;
      }
      setBusy(target, true);
      const id = state.collection.collection.id;
      const result = await api(`/api/collections/${id}/notifications`, { method: "PATCH", body: JSON.stringify({ enabled: !enabled }) });
      toast(result.notifications_enabled ? "Уведомления включены" : enabled ? "Уведомления выключены" : "Telegram не разрешил уведомления", !enabled && !result.notifications_enabled);
      return await openCollection(id, state.collectionTab, true);
    }
    if (action === "share-invite") {
      await shareCollection(state.collection.collection);
      return;
    }
    if (action === "transfer" || action === "remove-member") return memberActionSheet(action === "transfer" ? "transfer" : "remove");
    if (action === "cancel-transaction") {
      const fromGlobalHistory = target.dataset.return === "global";
      const question = fromGlobalHistory
        ? "Удалить транзакцию? Она останется в истории с отметкой об отмене."
        : "Отменить транзакцию? Она останется в истории.";
      if (!await confirmAction(question)) return;
      setBusy(target, true);
      const result = await api(`/api/transactions/${target.dataset.id}/cancel`, { method: "POST", body: "{}" });
      reportToast(result, fromGlobalHistory ? "Транзакция удалена" : "Транзакция отменена");
      if (fromGlobalHistory) {
        await reloadBootstrap();
        return await renderHistory();
      }
      return await refreshCurrent("history");
    }
    if (action === "confirm-repayment") {
      if (!await confirmAction("Подтвердить, что деньги получены?")) return;
      setBusy(target, true);
      const result = await api(`/api/transactions/${target.dataset.id}/confirm`, { method: "POST", body: "{}" });
      reportToast(result, "Получение подтверждено");
      if (target.dataset.return === "global") {
        await reloadBootstrap();
        return await renderHistory();
      }
      return await refreshCurrent("history");
    }
    if (action === "reject-repayment") {
      if (!await confirmAction("Отклонить получение? Возврат не будет учтён в балансах.")) return;
      setBusy(target, true);
      const result = await api(`/api/transactions/${target.dataset.id}/reject`, { method: "POST", body: "{}" });
      reportToast(result, "Получение отклонено");
      if (target.dataset.return === "global") {
        await reloadBootstrap();
        return await renderHistory();
      }
      return await refreshCurrent("history");
    }
    if (action === "leave" || action === "archive" || action === "restore") {
      const prompts = { leave: "Выйти из сбора? Это возможно только при нулевом балансе.", archive: "Завершить сбор и отправить его в архив на 30 дней?", restore: "Восстановить сбор?" };
      if (!await confirmAction(prompts[action])) return;
      setBusy(target, true);
      const id = state.collection.collection.id;
      const result = await api(`/api/collections/${id}/${action}`, { method: "POST", body: "{}" });
      reportToast(result, action === "leave" ? "Вы вышли из сбора" : action === "archive" ? "Сбор в архиве" : "Сбор восстановлен");
      await reloadBootstrap();
      await renderCollections();
    }
    if (action === "delete-collection") {
      if (!await confirmAction("Удалить архивный сбор навсегда? Все затраты, возвраты и история этого сбора будут удалены без возможности восстановления.")) return;
      setBusy(target, true);
      const id = state.collection.collection.id;
      const result = await api(`/api/collections/${id}`, { method: "DELETE" });
      reportToast(result, "Сбор удалён");
      state.collection = null;
      await reloadBootstrap();
      await renderCollections();
    }
  } catch (error) {
    haptic("heavy");
    toast(error.message, true);
  } finally {
    setBusy(target, false);
  }
});

app.addEventListener("change", async (event) => {
  if (event.target.dataset.notificationPref && !state.busy) {
    const preferences = {};
    app.querySelectorAll("[data-notification-pref]").forEach((input) => {
      preferences[input.dataset.notificationPref] = input.checked;
    });
    try {
      state.busy = true;
      await api("/api/me/notifications", { method: "PATCH", body: JSON.stringify(preferences) });
      state.bootstrap.user.notification_preferences = preferences;
      haptic();
      toast("Настройки уведомлений сохранены");
    } catch (error) {
      event.target.checked = !event.target.checked;
      toast(error.message, true);
    } finally {
      state.busy = false;
    }
    return;
  }
  if (event.target.id !== "preferred-currency" || state.busy) return;
  try {
    state.busy = true;
    await api("/api/me/currency", {
      method: "PATCH",
      body: JSON.stringify({ currency: event.target.value }),
    });
    await reloadBootstrap();
    haptic();
    toast(`Общий баланс будет показан в ${event.target.value}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.busy = false;
  }
});

sheet.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-action]");
  if (!target || state.busy) return;
  if (target.dataset.action === "open-user") {
    event.preventDefault();
    openTelegramUser(target);
    return;
  }
  if (target.dataset.action === "prompt-confirm-repayment" || target.dataset.action === "prompt-reject-repayment") {
    const confirming = target.dataset.action === "prompt-confirm-repayment";
    const question = confirming
      ? "Подтвердить, что деньги получены?"
      : "Отклонить получение? Возврат не будет учтён в балансах.";
    if (!await confirmAction(question)) return;
    try {
      setBusy(target, true);
      const endpoint = confirming ? "confirm" : "reject";
      const result = await api(`/api/transactions/${target.dataset.id}/${endpoint}`, { method: "POST", body: "{}" });
      closeSheet();
      reportToast(result, confirming ? "Получение подтверждено" : "Получение отклонено");
      await refreshVisibleView();
    } catch (error) {
      haptic("heavy");
      toast(error.message, true);
    } finally {
      setBusy(target, false);
    }
    return;
  }
  if (target.dataset.action === "close-sheet") closeSheet();
  if (target.dataset.action === "add-payment-method") {
    const container = sheet.querySelector("#payment-method-editors");
    if (container.children.length >= 10) return toast("Можно добавить не более 10 способов", true);
    container.insertAdjacentHTML("beforeend", paymentMethodEditor());
    haptic();
    return;
  }
  if (target.dataset.action === "remove-payment-method") {
    target.closest(".payment-method-editor")?.remove();
    haptic();
    return;
  }
  if (target.dataset.action === "share-created") {
    const collection = state.collection?.collection;
    if (collection) await shareCollection(collection);
    closeSheet();
    return;
  }
  if (target.dataset.action === "copy-payment") {
    try {
      await copyText(decodeURIComponent(target.dataset.value || ""));
      haptic();
      toast("Платёжные реквизиты скопированы");
    } catch (error) {
      toast("Не удалось скопировать реквизиты", true);
    }
    return;
  }
  if (target.dataset.action === "repay-max") {
    const option = sheet.querySelector('[name="creditor_id"] option:checked');
    const amount = sheet.querySelector('[name="amount"]');
    if (option && amount) amount.value = (Number(option.dataset.max) / 100).toFixed(2).replace(".", ",");
    haptic();
    return;
  }
  if (target.dataset.action === "select-all") {
    sheet.querySelectorAll('input[name="participant"]').forEach((input) => { input.checked = true; });
    haptic();
  }
  if (target.dataset.action === "add-bot-group") {
    const url = `https://t.me/${state.bootstrap.bot_username}?startgroup=shakeonit`;
    if (tg?.openTelegramLink) tg.openTelegramLink(url);
    else window.location.href = url;
    return;
  }
  if (target.dataset.action === "refresh-groups") {
    try {
      setBusy(target, true);
      await reloadBootstrap();
      createSheet();
      toast("Список групп обновлён");
    } catch (error) {
      toast(error.message, true);
    } finally {
      setBusy(target, false);
    }
    return;
  }
});

sheet.addEventListener("change", (event) => {
  if (event.target.dataset.action !== "repay-creditor") return;
  sheet.querySelectorAll("[data-payment]").forEach((node) => {
    node.hidden = node.dataset.payment !== event.target.value;
  });
});

sheet.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy) return;
  const form = event.target;
  const button = form.querySelector('button[type="submit"]');
  const values = new FormData(form);
  setBusy(button, true);
  try {
    let result;
    if (form.id === "create-form") {
      const chatId = Number(values.get("chat_id"));
      let subscribe = false;
      if (chatId === 0) {
        subscribe = await requestWritePermission();
        if (!subscribe) throw new Error("Разрешите боту присылать уведомления для сбора без группы");
      }
      result = await api("/api/collections", { method: "POST", body: JSON.stringify({ chat_id: chatId, title: values.get("title"), currency: values.get("currency"), subscribe }) });
      closeSheet();
      if (chatId === 0) toast(result.notifications_enabled ? "Личный сбор создан · уведомления включены" : "Сбор создан, но уведомления не включены", !result.notifications_enabled);
      else reportToast(result, "Сбор создан");
      await reloadBootstrap();
      await openCollection(result.collection_id, "overview", true);
      createdCollectionInviteSheet(state.collection.collection);
      return;
    }
    if (form.id === "expense-form") {
      const participantIds = [...form.querySelectorAll('input[name="participant"]:checked')].map((input) => Number(input.value));
      if (!participantIds.length) throw new Error("Выберите хотя бы одного участника");
      result = await api(`/api/collections/${state.collection.collection.id}/expenses`, { method: "POST", body: JSON.stringify({ amount: values.get("amount"), comment: values.get("comment"), participant_ids: participantIds }) });
      closeSheet(); reportToast(result, "Затрата добавлена"); return await refreshCurrent("overview");
    }
    if (form.id === "repay-form") {
      result = await api(`/api/collections/${state.collection.collection.id}/repayments`, { method: "POST", body: JSON.stringify({ amount: values.get("amount"), comment: values.get("comment"), creditor_id: Number(values.get("creditor_id")) }) });
      closeSheet(); reportToast(result, "Возврат записан"); return await refreshCurrent("overview");
    }
    if (form.id === "edit-form") {
      const payload = { amount: values.get("amount"), comment: values.get("comment") };
      const participantInputs = [...form.querySelectorAll('input[name="participant"]')];
      if (participantInputs.length) {
        payload.participant_ids = participantInputs.filter((input) => input.checked).map((input) => Number(input.value));
        if (!payload.participant_ids.length) throw new Error("Выберите хотя бы одного участника");
      }
      result = await api(`/api/transactions/${form.dataset.id}`, { method: "PATCH", body: JSON.stringify(payload) });
      closeSheet();
      reportToast(result, "Транзакция обновлена");
      if (form.dataset.return === "global") return await renderHistory();
      return await refreshCurrent("history");
    }
    if (form.id === "payment-form") {
      const banks = values.getAll("method_bank");
      const details = values.getAll("method_details");
      const paymentMethods = details.map((value, index) => ({ bank_name: banks[index] || "", details: value })).filter((method) => method.details.trim());
      await api("/api/me/payment-methods", { method: "PUT", body: JSON.stringify({ payment_methods: paymentMethods }) });
      closeSheet(); haptic(); toast("Платежные данные сохранены"); await reloadBootstrap(); return renderProfile();
    }
    if (form.id === "name-form") {
      await api("/api/me/name", { method: "PATCH", body: JSON.stringify({ full_name: values.get("full_name") }) });
      closeSheet(); haptic(); toast("Имя сохранено"); await reloadBootstrap(); return renderProfile();
    }
    if (form.id === "member-action-form") {
      const type = form.dataset.type;
      const endpoint = type === "transfer" ? "transfer" : "remove";
      result = await api(`/api/collections/${state.collection.collection.id}/${endpoint}`, { method: "POST", body: JSON.stringify({ user_id: Number(values.get("user_id")) }) });
      closeSheet(); reportToast(result, type === "transfer" ? "Роль передана" : "Участник удалён"); return await refreshCurrent("members");
    }
  } catch (error) {
    haptic("heavy");
    toast(error.message, true);
  } finally {
    setBusy(button, false);
  }
});

nav.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-nav]");
  if (!button || state.busy) return;
  try {
    rememberView(button.dataset.nav);
    if (button.dataset.nav === "collections") await renderCollections();
    if (button.dataset.nav === "balance") await renderBalance();
    if (button.dataset.nav === "history") await renderHistory();
    if (button.dataset.nav === "profile") renderProfile();
  } catch (error) { toast(error.message, true); }
});

avatar.addEventListener("click", () => {
  rememberView("profile");
  renderProfile();
});
document.getElementById("sheet-backdrop").addEventListener("click", closeSheet);
tg?.BackButton?.onClick(() => navigateBack(true));

app.addEventListener("touchstart", (event) => {
  const row = event.target.closest(".collection-swipe-row");
  const touch = event.changedTouches[0];
  if (!row || !touch || event.target.closest("button.swipe-danger-action")) return;
  state.collectionSwipe = { row, x: touch.clientX, y: touch.clientY };
}, { passive: true });

app.addEventListener("touchmove", (event) => {
  const start = state.collectionSwipe;
  const touch = event.changedTouches[0];
  if (!start || !touch) return;
  const dx = touch.clientX - start.x;
  const dy = Math.abs(touch.clientY - start.y);
  if (dy > Math.abs(dx)) return;
  const offset = Math.max(-104, Math.min(0, dx));
  start.row.style.setProperty("--swipe-x", `${offset}px`);
}, { passive: true });

app.addEventListener("touchend", (event) => {
  const start = state.collectionSwipe;
  state.collectionSwipe = null;
  if (!start) return;
  const touch = event.changedTouches[0];
  const reveal = touch && touch.clientX - start.x < -54;
  app.querySelectorAll(".collection-swipe-row.revealed").forEach((row) => {
    if (row !== start.row) row.classList.remove("revealed");
  });
  start.row.classList.toggle("revealed", Boolean(reveal));
  start.row.style.removeProperty("--swipe-x");
  if (reveal) haptic();
}, { passive: true });

document.addEventListener("touchstart", (event) => {
  const touch = event.changedTouches[0];
  if (!touch || touch.clientX > 28 || event.target.closest("input, textarea, select")) return;
  state.swipeStart = { x: touch.clientX, y: touch.clientY, at: performance.now() };
}, { passive: true });

document.addEventListener("touchend", async (event) => {
  const start = state.swipeStart;
  state.swipeStart = null;
  if (!start) return;
  const touch = event.changedTouches[0];
  if (!touch) return;
  const dx = touch.clientX - start.x;
  const dy = Math.abs(touch.clientY - start.y);
  if (dx >= 72 && dy <= 64 && performance.now() - start.at <= 700) {
    haptic();
    await navigateBack(false);
  }
}, { passive: true });

async function init() {
  tg?.ready();
  tg?.expand();
  tg?.setHeaderColor?.("bg_color");
  tg?.setBackgroundColor?.("bg_color");
  if (!tg?.initData) {
    nav.hidden = true;
    app.innerHTML = `<section class="auth-error"><span class="empty-icon">🔐</span><h2>Нужен защищённый запуск</h2><p class="row-note">Telegram открыл старую кнопку без данных профиля. Нажмите ниже — вход произойдёт автоматически, без логина и пароля.</p><div class="sheet-actions"><button class="primary-button" type="button" id="secure-open">Открыть в Telegram</button></div></section>`;
    document.getElementById("secure-open")?.addEventListener("click", () => {
      const startParam = state.launchIntent === "create" ? "create" : "app";
      const url = `https://t.me/${botUsername}?startapp=${startParam}`;
      if (tg?.openTelegramLink) tg.openTelegramLink(url);
      else window.location.href = url;
    });
    return;
  }
  try {
    await reloadBootstrap();
    if (state.bootstrap.is_new_user) {
      renderWelcome();
    } else {
      await continueAfterWelcome();
    }
    scheduleSync();
  } catch (error) {
    nav.hidden = true;
    app.innerHTML = `<section class="auth-error"><span class="empty-icon">↻</span><h2>Не удалось открыть приложение</h2><p class="row-note">${e(error.message)}</p><div class="sheet-actions"><button class="primary-button" type="button" id="retry">Попробовать снова</button></div></section>`;
    document.getElementById("retry")?.addEventListener("click", () => window.location.reload());
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") checkForUpdates(true);
});
window.addEventListener("focus", () => checkForUpdates(true));

init();
