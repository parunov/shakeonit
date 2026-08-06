"use strict";

const tg = window.Telegram?.WebApp;
const app = document.getElementById("app");
const title = document.getElementById("page-title");
const avatar = document.getElementById("avatar");
const nav = document.getElementById("bottom-nav");
const sheetLayer = document.getElementById("sheet-layer");
const sheet = document.getElementById("sheet");
const toastNode = document.getElementById("toast");
const botUsername = document.querySelector('meta[name="telegram-bot-username"]')?.content;
const launchParams = new URLSearchParams(window.location.search);

const state = {
  bootstrap: null,
  collection: null,
  collectionReturn: "collections",
  collectionTab: "overview",
  details: new Map(),
  nav: "collections",
  busy: false,
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
  return new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
  }).format((amount || 0) / 100);
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
    : new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(date);
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
  if (result.notifications_sent > 0) {
    toast(`${fallback} · подписчики уведомлены: ${result.notifications_sent}`);
  } else {
    toast(result.report_sent === false ? fallback : `${fallback} · отчёт отправлен в чат`);
  }
}

async function reloadBootstrap() {
  state.bootstrap = await api("/api/bootstrap");
  state.details.clear();
  const initials = state.bootstrap.user.full_name.split(/\s+/).map((part) => part[0]).join("").slice(0, 2);
  avatar.textContent = initials || "S";
}

function empty(icon, text) {
  return `<div class="empty"><span class="empty-icon">${icon}</span>${e(text)}</div>`;
}

function collectionCards(rows) {
  if (!rows.length) return empty("🌿", "Сборов пока нет. Создайте первый с группой или без неё.");
  return `<div class="card-list">${rows.map((item) => `
    <button class="card collection-card" type="button" data-action="${item.is_participant === false ? "preview-collection" : "open-collection"}" data-id="${item.id}">
      <span class="collection-icon">${item.status === "archived" ? "📦" : "🧾"}</span>
      <span>
        <span class="card-title">${e(item.title)}</span>
        <span class="card-subtitle">${e(item.currency)} · ${item.participants_count ?? "—"} участников${item.is_personal ? " · без группы" : ""}${item.status === "archived" ? " · архив" : item.is_participant === false ? " · можно участвовать" : ""}</span>
      </span>
      <span class="chevron">›</span>
    </button>`).join("")}</div>`;
}

function renderCollections() {
  state.nav = "collections";
  state.collection = null;
  title.textContent = state.bootstrap.context_chat_id ? "Сборы группы" : "Сборы";
  tg?.BackButton?.hide();
  const active = state.bootstrap.collections.filter((item) => item.status === "active");
  const archived = state.bootstrap.collections.filter((item) => item.status === "archived");
  app.innerHTML = `
    <section class="hero">
      <div class="hero-label">${state.bootstrap.context_chat_id ? "ЭТА TELEGRAM-ГРУППА" : "АКТИВНЫЕ СБОРЫ"}</div>
      <div class="hero-value">${active.length}</div>
      <div class="hero-meta">${state.bootstrap.context_chat_id ? "Можно открыть сбор или участвовать в один шаг" : "Все расчёты синхронизированы"}</div>
    </section>
    <div class="section-head"><h2>Текущие</h2><button class="text-button" type="button" data-action="create">+ Новый</button></div>
    ${collectionCards(active)}
    ${archived.length ? `<div class="section-head"><h2>Архив</h2></div>${collectionCards(archived)}` : ""}`;
  updateNav();
}

async function renderBalance() {
  state.nav = "balance";
  state.collection = null;
  title.textContent = "Мой баланс";
  tg?.BackButton?.hide();
  app.innerHTML = `<section class="loading-card"><div class="spinner"></div><p>Считаем ваш баланс…</p></section>`;
  updateNav();
  const rows = state.bootstrap.collections.filter((item) => item.status === "active");
  const details = await Promise.all(rows.map((item) => loadDetails(item.id)));
  let exchange = null;
  try {
    exchange = await api("/api/rates");
  } catch (error) {
    toast("Курсы валют временно недоступны — показываю исходные суммы", true);
  }
  const totals = new Map();
  const cards = details.map((data) => {
    const mine = data.balances.find((item) => item.user_id === state.bootstrap.user.id)?.amount || 0;
    const status = balanceStatus(mine);
    totals.set(data.collection.currency, (totals.get(data.collection.currency) || 0) + mine);
    return `
      <button class="card collection-card" type="button" data-action="open-collection" data-id="${data.collection.id}">
        <span class="collection-icon status-icon" aria-hidden="true">${status.icon}</span>
        <span><span class="card-title">${e(data.collection.title)}</span><span class="card-subtitle">${status.label}</span></span>
        <span class="amount ${mine > 0 ? "positive" : mine < 0 ? "negative" : ""}">${money(Math.abs(mine), data.collection.currency)}</span>
      </button>`;
  });
  const preferred = state.bootstrap.user.preferred_currency;
  const convertedTotal = exchange ? Math.round([...totals.entries()].reduce((sum, [currency, amount]) => sum + amount * exchange.rates[currency] / exchange.rates[preferred], 0)) : null;
  const originalLabel = [...totals.entries()].map(([currency, amount]) => `${amount >= 0 ? "+" : "−"}${money(Math.abs(amount), currency)}`).join(" · ") || "0";
  const netLabel = convertedTotal === null ? originalLabel : `${convertedTotal >= 0 ? "+" : "−"}${money(Math.abs(convertedTotal), preferred)}`;
  app.innerHTML = `
    <section class="hero">
      <div class="hero-label">ОБЩИЙ БАЛАНС · ${e(preferred)}</div>
      <div class="hero-value">${netLabel}</div>
      <div class="hero-meta">${exchange ? `≈ по официальному курсу НБРБ · ${e(originalLabel)}` : "По активным сборам · без конвертации"}</div>
    </section>
    <div class="section-head"><h2>По сборам</h2></div>
    ${cards.length ? `<div class="card-list">${cards.join("")}</div>` : empty("✅", "Активных долгов нет")}`;
}

function renderProfile() {
  state.nav = "profile";
  state.collection = null;
  title.textContent = "Профиль";
  tg?.BackButton?.hide();
  const user = state.bootstrap.user;
  app.innerHTML = `
    <section class="card member-row">
      <div class="row-between"><div><div class="row-title">${e(user.full_name)}</div><div class="row-note">${user.username ? `@${e(user.username)}` : `Telegram ID ${user.id}`}</div></div><span class="pill">Подключён</span></div>
    </section>
    <div class="section-head"><h2>Общая валюта баланса</h2></div>
    <section class="card member-row">
      <div class="row-note">Используется только для общего итога. Суммы внутри сборов не меняются.</div>
      <label class="field compact-field"><span>Показывать общий баланс в</span><select id="preferred-currency">${state.bootstrap.currencies.map((currency) => `<option ${currency === user.preferred_currency ? "selected" : ""}>${currency}</option>`).join("")}</select></label>
    </section>
    <div class="section-head"><h2>Платежные данные</h2><button class="text-button" type="button" data-action="payment">Изменить</button></div>
    <section class="card member-row">
      <div class="row-note">Участники ваших сборов увидят эти данные рядом с именем.</div>
      <div class="row-title">${e(user.payment_details || "Пока не добавлены")}</div>
    </section>
    <div class="status-banner">🔒 Вход подтверждается Telegram. Пароли и отдельная регистрация не нужны.</div>`;
  updateNav();
}

async function renderHistory() {
  state.nav = "history";
  state.collection = null;
  title.textContent = "История";
  tg?.BackButton?.hide();
  app.innerHTML = `<section class="loading-card"><div class="spinner"></div><p>Собираем всю историю…</p></section>`;
  updateNav();
  const data = await api("/api/history");
  const eventLabels = {
    created: "создал сбор", joined: "вступил в сбор", left: "вышел из сбора",
    member_removed: "удалил участника", admin_transferred: "передал роль администратора",
    archived: "завершил сбор", restored: "восстановил сбор",
    funds_requested: "вежливо запросил завершить расчёт",
  };
  const transactions = data.transactions.map((item) => {
    const isConfirmedRepayment = item.kind === "repayment" && item.confirmation_status === "confirmed";
    const canEdit = item.is_participant && item.status === "active" && !isConfirmedRepayment && item.collection_status === "active" && (item.creator_id === state.bootstrap.user.id || item.collection_admin_id === state.bootstrap.user.id);
    const canCancel = item.is_participant && item.status === "active" && item.collection_status === "active" && (item.collection_admin_id === state.bootstrap.user.id || (item.creator_id === state.bootstrap.user.id && !isConfirmedRepayment));
    const status = item.status === "cancelled" ? '<span class="pill cancelled">отменено</span>' : item.kind === "repayment" && item.confirmation_status === "pending" ? '<span class="pill pending">ожидает</span>' : item.kind === "repayment" ? '<span class="pill">подтверждено</span>' : "";
    const shares = item.kind === "expense" && item.shares.length ? `<div class="share-breakdown">${item.shares.map((share) => `<div class="row-note">${e(share.full_name)} — ${money(share.amount, item.currency)}</div>`).join("")}</div>` : "";
    const collectionTitle = item.is_participant ? `<button class="collection-link" type="button" data-action="open-collection" data-id="${item.collection_id}">${e(item.collection_title)}</button>` : `<span>${e(item.collection_title)}</span>`;
    const actions = canEdit || canCancel ? `<div class="transaction-actions">${canEdit ? `<button class="mini-button" type="button" data-action="edit-history-transaction" data-id="${item.id}" data-collection-id="${item.collection_id}">Изменить</button>` : ""}${canCancel ? `<button class="mini-button danger" type="button" data-action="cancel-transaction" data-id="${item.id}" data-return="global">Удалить</button>` : ""}</div>` : "";
    return `<article class="history-row"><div class="row-between"><div><div class="row-title history-collection-title"><span>${item.kind === "expense" ? "💸" : "🤝"}</span>${collectionTitle}</div><div class="row-note">${e(item.creator_name)} · ${shortDate(item.created_at)}</div></div><div class="amount">${money(item.amount, item.currency)}</div></div><div class="row-note">${e(item.comment || (item.kind === "expense" ? "Затрата" : `Возврат → ${item.counterparty_name}`))} ${status}</div>${shares}${actions}</article>`;
  }).join("");
  const events = data.events.map((item) => `<article class="history-row"><div class="row-title">${item.is_participant ? `<button class="collection-link" type="button" data-action="open-collection" data-id="${item.collection_id}">${e(item.collection_title)}</button>` : e(item.collection_title)}</div><div class="row-note">${shortDate(item.created_at)} · ${e(item.actor_name)} ${e(eventLabels[item.kind] || item.kind)}${item.target_name && item.target_name !== item.actor_name ? ` · ${e(item.target_name)}` : ""}</div></article>`).join("");
  app.innerHTML = `<section class="hero"><div class="hero-label">ЛЕНТА ДЕЙСТВИЙ</div><div class="hero-value">${data.transactions.length}</div><div class="hero-meta">операций во всех ваших сборах</div></section><div class="section-head"><h2>Транзакции</h2></div>${transactions ? `<div class="card">${transactions}</div>` : empty("📜", "Транзакций пока нет")}<div class="section-head"><h2>История сборов</h2></div>${events ? `<div class="card">${events}</div>` : empty("🧾", "Событий пока нет")}`;
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

function continueAfterWelcome() {
  nav.hidden = false;
  if (state.bootstrap.invitation?.is_participant) {
    return openCollection(state.bootstrap.invitation.collection.id);
  }
  if (state.bootstrap.invitation) return renderInvitation();
  if (state.launchIntent === "create") {
    state.launchIntent = null;
    renderCollections();
    return createSheet();
  }
  return renderCollections();
}

function updateNav() {
  nav.querySelectorAll("button").forEach((button) => button.classList.toggle("active", button.dataset.nav === state.nav));
}

async function loadDetails(id, force = false) {
  if (!force && state.details.has(Number(id))) return state.details.get(Number(id));
  const data = await api(`/api/collections/${id}`);
  state.details.set(Number(id), data);
  return data;
}

async function openCollection(id, tab = "overview", force = false) {
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
    ${collection.status === "active" ? `<button class="request-funds" type="button" data-action="request-funds" ${myDebtors.length ? "" : "disabled"}><span>🔔</span><span><b>Запросить средства</b><small>${myDebtors.length ? `Вежливо напомнить должникам · ${myDebtors.length}` : "Сейчас вам никто не должен"}</small></span><i>›</i></button>` : ""}
    ${collection.status === "active" ? `<button class="notification-toggle ${data.notifications_enabled ? "enabled" : ""}" type="button" data-action="toggle-notifications" data-enabled="${data.notifications_enabled}"><span>${data.notifications_enabled ? "🔔" : "🔕"}</span><span><b>${data.notifications_enabled ? "Уведомления включены" : "Получать уведомления"}</b><small>${data.notifications_enabled ? "Бот сообщит о новых операциях в личном чате" : "Даже если вас нет в Telegram-группе"}</small></span><i>›</i></button>` : ""}
    <div class="tabs">${tabs.map(([key, label]) => `<button type="button" data-action="tab" data-tab="${key}" class="${state.collectionTab === key ? "active" : ""}">${label}</button>`).join("")}</div>
    <section class="panel">${renderCollectionPanel(data, isAdmin)}</section>`;
}

function renderCollectionPanel(data, isAdmin) {
  const collection = data.collection;
  if (state.collectionTab === "overview") {
    const balances = data.participants.map((member) => {
      const amount = data.balances.find((item) => item.user_id === member.id)?.amount || 0;
      return `<div class="balance-row"><div class="row-between"><div><div class="row-title">${e(member.full_name)}${member.id === state.bootstrap.user.id ? " · вы" : ""}${member.active === false ? " · вышел" : ""}</div><div class="row-note">${amount > 0 ? "должны участники" : amount < 0 ? "должен участникам" : "расчёт закрыт"}</div></div><div class="amount ${amount > 0 ? "positive" : amount < 0 ? "negative" : ""}">${amount > 0 ? "+" : amount < 0 ? "−" : ""}${money(Math.abs(amount), collection.currency)}</div></div></div>`;
    }).join("");
    const debts = data.debts.map((debt) => `<div class="debt-row"><div class="row-title">${e(debt.debtor_name)} → ${e(debt.creditor_name)}</div><div class="row-note">Перевести ${money(debt.amount, collection.currency)}</div></div>`).join("");
    return `<div class="section-head"><h2>Балансы</h2></div><div class="card">${balances}</div><div class="section-head"><h2>Кто кому</h2></div><div class="card">${debts || `<div class="debt-row">✅ Никто никому не должен</div>`}</div>`;
  }
  if (state.collectionTab === "history") {
    const transactions = data.history.map((item) => {
      const isConfirmedRepayment = item.kind === "repayment" && item.confirmation_status === "confirmed";
      const canEdit = item.status === "active" && !isConfirmedRepayment && collection.status === "active" && (isAdmin || item.creator_id === state.bootstrap.user.id);
      const canCancel = item.status === "active" && collection.status === "active" && (isAdmin || (item.creator_id === state.bootstrap.user.id && !isConfirmedRepayment));
      const canConfirm = item.kind === "repayment" && item.status === "active" && item.confirmation_status === "pending" && item.counterparty_id === state.bootstrap.user.id;
      const subject = item.kind === "expense" ? (item.comment || "Затрата") : `Возврат → ${item.counterparty_name}`;
      const status = item.status === "cancelled" ? '<span class="pill cancelled">отменено</span>' : item.kind === "repayment" && item.confirmation_status === "pending" ? '<span class="pill pending">ожидает подтверждения</span>' : item.kind === "repayment" ? '<span class="pill">подтверждено</span>' : "";
      const shares = item.kind === "expense" ? `<div class="share-breakdown">${item.shares.map((share) => `<div class="row-note">${e(share.full_name)} — ${money(share.amount, collection.currency)}</div>`).join("")}</div>` : "";
      return `<article class="history-row"><div class="row-between"><div><div class="row-title">${item.kind === "expense" ? "💸" : "🤝"} ${e(subject)}</div><div class="row-note">${e(item.creator_name)} · ${shortDate(item.created_at)}</div></div><div class="amount">${money(item.amount, collection.currency)}</div></div><div class="row-note">${item.kind === "expense" ? "Распределение по людям" : "Фактический перевод"} ${status}</div>${shares}${canConfirm ? `<div class="transaction-actions"><button class="mini-button" type="button" data-action="confirm-repayment" data-id="${item.id}">✅ Подтвердить получение</button></div>` : ""}${canEdit || canCancel ? `<div class="transaction-actions">${canEdit ? `<button class="mini-button" type="button" data-action="edit-transaction" data-id="${item.id}">Изменить</button>` : ""}${canCancel ? `<button class="mini-button danger" type="button" data-action="cancel-transaction" data-id="${item.id}">Отменить</button>` : ""}</div>` : ""}</article>`;
    }).join("");
    const eventLabels = {
      created: "создал сбор", joined: "вступил в сбор", left: "вышел из сбора",
      member_removed: "удалил участника", admin_transferred: "передал роль администратора",
      archived: "завершил сбор", restored: "восстановил сбор",
      funds_requested: "вежливо запросил завершить расчёт",
    };
    const events = data.events.map((item) => `<article class="history-row"><div class="row-title">${shortDate(item.created_at)} · ${e(item.actor_name)} ${e(eventLabels[item.kind] || item.kind)}</div>${item.target_name && item.target_name !== item.actor_name ? `<div class="row-note">Участник: ${e(item.target_name)}</div>` : ""}</article>`).join("");
    if (!transactions && !events) return empty("📜", "История пока пуста");
    return `<div class="section-head"><h2>Транзакции</h2></div>${transactions ? `<div class="card">${transactions}</div>` : empty("📜", "Транзакций пока нет")}<div class="section-head"><h2>Участники и сбор</h2></div>${events ? `<div class="card">${events}</div>` : empty("👥", "Событий пока нет")}`;
  }
  if (state.collectionTab === "members") {
    const invite = collection.status === "active" ? `<button class="invite-button" type="button" data-action="share-invite">👥<span><b>Пригласить друзей</b><small>Выбрать человека или Telegram-группу</small></span><i>›</i></button>` : "";
    const activeParticipants = data.participants.filter((member) => member.active !== false);
    const members = activeParticipants.map((member) => `<div class="member-row"><div class="row-between"><div><div class="row-title">${e(member.full_name)} ${member.is_admin ? "👑" : ""}</div><div class="row-note">${member.username ? `@${e(member.username)}` : `ID ${member.id}`}${member.payment_details ? `<br>💳 ${e(member.payment_details)}` : ""}</div></div></div></div>`).join("");
    return `${invite}<div class="section-head"><h2>Участники · ${activeParticipants.length}</h2></div><div class="card">${members}</div>${collection.status === "active" && !isAdmin ? '<div class="sheet-actions"><button class="danger-button" type="button" data-action="leave">Выйти из сбора</button></div>' : ""}`;
  }
  return `<div class="card member-row"><div class="row-title">Администратор сбора</div><div class="row-note">Передача роли и удаление участников доступны только при соблюдении балансов.</div></div><div class="sheet-actions">${collection.status === "active" ? '<button class="secondary-button" type="button" data-action="transfer">Передать администратора</button><button class="secondary-button" type="button" data-action="remove-member">Удалить участника</button><button class="danger-button" type="button" data-action="archive">Завершить и архивировать</button>' : '<button class="primary-button" type="button" data-action="restore">Восстановить сбор</button>'}</div>`;
}

function createSheet() {
  const chats = [...state.bootstrap.chats];
  if (state.bootstrap.context_chat_id && !chats.some((chat) => chat.chat_id === state.bootstrap.context_chat_id)) {
    chats.unshift({ chat_id: state.bootstrap.context_chat_id, label: "Текущая Telegram-группа" });
  }
  const destinations = [...chats, { chat_id: 0, label: "Без Telegram-группы · уведомления в боте" }];
  showSheet(`<h2>Новый сбор</h2><p class="sheet-intro">Можно связать сбор с Telegram-группой или вести его только через личные уведомления бота.</p><form id="create-form"><label class="field"><span>Где вести сбор</span><select name="chat_id" required>${destinations.map((chat) => `<option value="${chat.chat_id}">${e(chat.label)}</option>`).join("")}</select></label><label class="field"><span>Название</span><input name="title" minlength="2" maxlength="80" placeholder="Например, Поездка в Варшаву" required></label><label class="field"><span>Валюта</span><select name="currency">${state.bootstrap.currencies.map((currency) => `<option>${currency}</option>`).join("")}</select></label><div class="sheet-actions"><button class="primary-button" type="submit">Создать сбор</button><button class="secondary-button" type="button" data-action="add-bot-group">Добавить бота в новую группу</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

function expenseSheet() {
  const data = state.collection;
  const activeParticipants = data.participants.filter((member) => member.active !== false);
  showSheet(`<h2>Добавить затрату</h2><p class="sheet-intro">Сумма будет поровну распределена между отмеченными людьми.</p><form id="expense-form"><label class="field"><span>Сумма · ${e(data.collection.currency)}</span><input name="amount" inputmode="decimal" placeholder="0,00" required></label><label class="field"><span>Комментарий</span><input name="comment" maxlength="200" placeholder="Например, билеты"></label><div class="row-between"><span class="row-title">На кого делим</span><button class="text-button" type="button" data-action="select-all">Выбрать всех</button></div><div class="check-list">${activeParticipants.map((member) => `<label class="check-row"><input type="checkbox" name="participant" value="${member.id}"><span>${e(member.full_name)}${member.id === state.bootstrap.user.id ? " · вы" : ""}</span></label>`).join("")}</div><div class="sheet-actions"><button class="primary-button" type="submit">Добавить затрату</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

function repaySheet() {
  const data = state.collection;
  const debts = data.debts.filter((debt) => debt.debtor_id === state.bootstrap.user.id);
  if (!debts.length) {
    toast("По текущему балансу у вас нет долгов");
    return;
  }
  const cards = debts.map((debt, index) => {
    const member = data.participants.find((item) => item.id === debt.creditor_id);
    return `<div class="payment-card" data-payment="${debt.creditor_id}" ${index ? "hidden" : ""}><b>💳 Данные для перевода</b><br>${e(member?.payment_details || "Получатель пока не добавил платежные данные")}</div>`;
  }).join("");
  showSheet(`<h2>Вернуть долг</h2><p class="sheet-intro">После записи получатель должен подтвердить деньги. До этого баланс не изменится.</p><form id="repay-form"><label class="field"><span>Получатель</span><select name="creditor_id" data-action="repay-creditor">${debts.map((debt) => `<option value="${debt.creditor_id}">${e(debt.creditor_name)} · до ${money(debt.amount, data.collection.currency)}</option>`).join("")}</select></label>${cards}<label class="field"><span>Переведено · ${e(data.collection.currency)}</span><input name="amount" inputmode="decimal" placeholder="0,00" required></label><label class="field"><span>Комментарий</span><input name="comment" maxlength="200" placeholder="Необязательно"></label><div class="sheet-actions"><button class="primary-button" type="submit">Отправить на подтверждение</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

function editSheet(transactionId, collectionData = state.collection, returnTo = "collection") {
  const item = collectionData.history.find((row) => row.id === Number(transactionId));
  const selectedIds = new Set(item.shares.map((share) => share.user_id));
  const candidates = collectionData.participants
    .filter((member) => member.active !== false)
    .map((member) => ({ ...member, active: true }));
  item.shares.forEach((share) => {
    if (!candidates.some((member) => member.id === share.user_id)) {
      candidates.push({ id: share.user_id, full_name: share.full_name, active: false });
    }
  });
  const participantEditor = item.kind === "expense" ? `<div class="row-between"><span class="row-title">На кого делим</span><button class="text-button" type="button" data-action="select-all">Выбрать всех</button></div><div class="check-list">${candidates.map((member) => `<label class="check-row"><input type="checkbox" name="participant" value="${member.id}" ${selectedIds.has(member.id) ? "checked" : ""}><span>${e(member.full_name)}${member.id === state.bootstrap.user.id ? " · вы" : ""}${member.active ? "" : " · вышел из сбора"}</span></label>`).join("")}</div>` : "";
  const intro = item.kind === "expense"
    ? "После сохранения сумма будет поровну распределена между отмеченными людьми, а балансы пересчитаются."
    : "Изменённый возврат останется на подтверждении у получателя.";
  showSheet(`<h2>Изменить транзакцию</h2><p class="sheet-intro">${intro}</p><form id="edit-form" data-id="${item.id}" data-return="${returnTo}"><label class="field"><span>Сумма · ${e(collectionData.collection.currency)}</span><input name="amount" inputmode="decimal" value="${(item.amount / 100).toFixed(2).replace(".", ",")}" required></label><label class="field"><span>Комментарий</span><input name="comment" maxlength="200" value="${e(item.comment)}"></label>${participantEditor}<div class="sheet-actions"><button class="primary-button" type="submit">Сохранить</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

function paymentSheet() {
  showSheet(`<h2>Платежные данные</h2><p class="sheet-intro">Например, телефон СБП или последние цифры карты. Не указывайте секретные коды.</p><form id="payment-form"><label class="field"><span>Реквизиты</span><textarea name="payment_details" maxlength="500" placeholder="Можно оставить пустым">${e(state.bootstrap.user.payment_details)}</textarea></label><div class="sheet-actions"><button class="primary-button" type="submit">Сохранить</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
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

app.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-action]");
  if (!target || state.busy) return;
  const action = target.dataset.action;
  try {
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
      reportToast(result, result.notifications_enabled ? "Вы участвуете · уведомления включены" : "Вы участвуете в сборе");
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
      const details = await loadDetails(target.dataset.collectionId, true);
      return editSheet(target.dataset.id, details, "global");
    }
    if (action === "request-funds") {
      if (!await confirmAction("Отправить всем вашим должникам вежливое напоминание о расчёте?")) return;
      setBusy(target, true);
      const result = await api(`/api/collections/${state.collection.collection.id}/request-funds`, { method: "POST", body: "{}" });
      if (result.failed_count) {
        toast(`Отправлено ${result.notifications_sent} из ${result.debtors_count}. Остальные ещё не подключили бота`, true);
      } else {
        toast(`Напоминание отправлено · ${result.notifications_sent}`);
      }
      return await refreshCurrent(state.collectionTab);
    }
    if (action === "payment") return paymentSheet();
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
      const collection = state.collection.collection;
      const inviteUrl = state.bootstrap.main_app_enabled
        ? `https://t.me/${state.bootstrap.bot_username}?startapp=collection_${collection.id}&mode=compact`
        : `https://t.me/${state.bootstrap.bot_username}?start=collection_${collection.id}`;
      const inviteText = state.bootstrap.main_app_enabled
        ? `Присоединяйся к сбору «${collection.title}» в ShakeOnIt. Ссылка сразу откроет приложение.`
        : `Присоединяйся к сбору «${collection.title}» в ShakeOnIt. Открой ссылку — бот добавит тебя автоматически.`;
      const shareUrl = `https://t.me/share/url?url=${encodeURIComponent(inviteUrl)}&text=${encodeURIComponent(inviteText)}`;
      haptic();
      if (tg?.openTelegramLink) tg.openTelegramLink(shareUrl);
      else window.location.href = shareUrl;
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
      await refreshCurrent("history");
    }
    if (action === "leave" || action === "archive" || action === "restore") {
      const prompts = { leave: "Выйти из сбора? Это возможно только при нулевом балансе.", archive: "Завершить сбор и отправить его в архив на 30 дней?", restore: "Восстановить сбор?" };
      if (!await confirmAction(prompts[action])) return;
      setBusy(target, true);
      const id = state.collection.collection.id;
      const result = await api(`/api/collections/${id}/${action}`, { method: "POST", body: "{}" });
      reportToast(result, action === "leave" ? "Вы вышли из сбора" : action === "archive" ? "Сбор в архиве" : "Сбор восстановлен");
      await reloadBootstrap();
      renderCollections();
    }
  } catch (error) {
    haptic("heavy");
    toast(error.message, true);
  } finally {
    setBusy(target, false);
  }
});

app.addEventListener("change", async (event) => {
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
  if (target.dataset.action === "close-sheet") closeSheet();
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
      return await openCollection(result.collection_id, "overview", true);
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
      await api("/api/me/payment", { method: "PATCH", body: JSON.stringify({ payment_details: values.get("payment_details") }) });
      closeSheet(); haptic(); toast("Платежные данные сохранены"); await reloadBootstrap(); return renderProfile();
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
    if (button.dataset.nav === "collections") renderCollections();
    if (button.dataset.nav === "balance") await renderBalance();
    if (button.dataset.nav === "history") await renderHistory();
    if (button.dataset.nav === "profile") renderProfile();
  } catch (error) { toast(error.message, true); }
});

avatar.addEventListener("click", renderProfile);
document.getElementById("sheet-backdrop").addEventListener("click", closeSheet);
tg?.BackButton?.onClick(async () => {
  if (!state.collection) return tg.close();
  const returnTo = state.collectionReturn;
  state.collection = null;
  if (returnTo === "history") return await renderHistory();
  if (returnTo === "balance") return await renderBalance();
  return renderCollections();
});

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
  } catch (error) {
    nav.hidden = true;
    app.innerHTML = `<section class="auth-error"><span class="empty-icon">↻</span><h2>Не удалось открыть приложение</h2><p class="row-note">${e(error.message)}</p><div class="sheet-actions"><button class="primary-button" type="button" id="retry">Попробовать снова</button></div></section>`;
    document.getElementById("retry")?.addEventListener("click", () => window.location.reload());
  }
}

init();
