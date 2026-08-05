"use strict";

const tg = window.Telegram?.WebApp;
const app = document.getElementById("app");
const title = document.getElementById("page-title");
const avatar = document.getElementById("avatar");
const nav = document.getElementById("bottom-nav");
const sheetLayer = document.getElementById("sheet-layer");
const sheet = document.getElementById("sheet");
const toastNode = document.getElementById("toast");

const state = {
  bootstrap: null,
  collection: null,
  collectionTab: "overview",
  details: new Map(),
  nav: "collections",
  busy: false,
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
  toast(result.report_sent === false ? `${fallback} · отчёт останется здесь` : `${fallback} · отчёт отправлен в чат`);
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
  if (!rows.length) return empty("🌿", "Сборов пока нет. Создайте первый в Telegram-группе.");
  return `<div class="card-list">${rows.map((item) => `
    <button class="card collection-card" type="button" data-action="open-collection" data-id="${item.id}">
      <span class="collection-icon">${item.status === "archived" ? "📦" : "🧾"}</span>
      <span>
        <span class="card-title">${e(item.title)}</span>
        <span class="card-subtitle">${e(item.currency)} · ${item.participants_count ?? "—"} участников${item.status === "archived" ? " · архив" : ""}</span>
      </span>
      <span class="chevron">›</span>
    </button>`).join("")}</div>`;
}

function renderCollections() {
  state.nav = "collections";
  state.collection = null;
  title.textContent = "Сборы";
  tg?.BackButton?.hide();
  const active = state.bootstrap.collections.filter((item) => item.status === "active");
  const archived = state.bootstrap.collections.filter((item) => item.status === "archived");
  app.innerHTML = `
    <section class="hero">
      <div class="hero-label">АКТИВНЫЕ СБОРЫ</div>
      <div class="hero-value">${active.length}</div>
      <div class="hero-meta">Все расчёты синхронизированы с ботом</div>
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
  const totals = new Map();
  const cards = details.map((data) => {
    const mine = data.balances.find((item) => item.user_id === state.bootstrap.user.id)?.amount || 0;
    totals.set(data.collection.currency, (totals.get(data.collection.currency) || 0) + mine);
    return `
      <button class="card collection-card" type="button" data-action="open-collection" data-id="${data.collection.id}">
        <span class="collection-icon">⚖️</span>
        <span><span class="card-title">${e(data.collection.title)}</span><span class="card-subtitle">${mine > 0 ? "вам должны" : mine < 0 ? "вы должны" : "всё закрыто"}</span></span>
        <span class="amount ${mine > 0 ? "positive" : mine < 0 ? "negative" : ""}">${money(Math.abs(mine), data.collection.currency)}</span>
      </button>`;
  });
  const netLabel = [...totals.entries()].map(([currency, amount]) => `${amount >= 0 ? "+" : "−"}${money(Math.abs(amount), currency)}`).join(" · ") || "0";
  app.innerHTML = `
    <section class="hero">
      <div class="hero-label">ЧИСТЫЙ БАЛАНС</div>
      <div class="hero-value">${netLabel}</div>
      <div class="hero-meta">По активным сборам · отдельно по каждой валюте</div>
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
    <div class="section-head"><h2>Платежные данные</h2><button class="text-button" type="button" data-action="payment">Изменить</button></div>
    <section class="card member-row">
      <div class="row-note">Участники ваших сборов увидят эти данные рядом с именем.</div>
      <div class="row-title">${e(user.payment_details || "Пока не добавлены")}</div>
    </section>
    <div class="status-banner">🔒 Вход подтверждается Telegram. Пароли и отдельная регистрация не нужны.</div>`;
  updateNav();
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
  const tabs = [
    ["overview", "Обзор"], ["history", "История"], ["members", "Люди"],
    ...(isAdmin ? [["admin", "Управление"]] : []),
  ];
  app.innerHTML = `
    <section class="hero">
      <div class="hero-label">ВСЕГО ЗАТРАТ · ${e(collection.currency)}</div>
      <div class="hero-value">${money(data.total, collection.currency)}</div>
      <div class="hero-meta">${data.participants.length} участников · ${collection.status === "active" ? "активен" : "в архиве"}</div>
    </section>
    ${collection.status === "active" ? `<div class="quick-actions"><button class="action-button" type="button" data-action="expense">💸 Добавить затрату</button><button class="action-button" type="button" data-action="repay">🤝 Вернуть долг</button></div>` : `<div class="status-banner">📦 Сбор находится в архиве. Балансы и история доступны без изменений.</div>`}
    <div class="status-banner">${myBalance > 0 ? `Вам должны <b>${money(myBalance, collection.currency)}</b>` : myBalance < 0 ? `Вы должны <b>${money(-myBalance, collection.currency)}</b>` : "✅ Ваш расчёт закрыт"}</div>
    <div class="tabs">${tabs.map(([key, label]) => `<button type="button" data-action="tab" data-tab="${key}" class="${state.collectionTab === key ? "active" : ""}">${label}</button>`).join("")}</div>
    <section class="panel">${renderCollectionPanel(data, isAdmin)}</section>`;
}

function renderCollectionPanel(data, isAdmin) {
  const collection = data.collection;
  if (state.collectionTab === "overview") {
    const balances = data.participants.map((member) => {
      const amount = data.balances.find((item) => item.user_id === member.id)?.amount || 0;
      return `<div class="balance-row"><div class="row-between"><div><div class="row-title">${e(member.full_name)}${member.id === state.bootstrap.user.id ? " · вы" : ""}</div><div class="row-note">${amount > 0 ? "должны участники" : amount < 0 ? "должен участникам" : "расчёт закрыт"}</div></div><div class="amount ${amount > 0 ? "positive" : amount < 0 ? "negative" : ""}">${amount > 0 ? "+" : amount < 0 ? "−" : ""}${money(Math.abs(amount), collection.currency)}</div></div></div>`;
    }).join("");
    const debts = data.debts.map((debt) => `<div class="debt-row"><div class="row-title">${e(debt.debtor_name)} → ${e(debt.creditor_name)}</div><div class="row-note">Перевести ${money(debt.amount, collection.currency)}</div></div>`).join("");
    return `<div class="section-head"><h2>Балансы</h2></div><div class="card">${balances}</div><div class="section-head"><h2>Кто кому</h2></div><div class="card">${debts || `<div class="debt-row">✅ Никто никому не должен</div>`}</div>`;
  }
  if (state.collectionTab === "history") {
    if (!data.history.length) return empty("📜", "История пока пуста");
    return `<div class="card">${data.history.map((item) => {
      const canEdit = item.status === "active" && collection.status === "active" && (isAdmin || item.creator_id === state.bootstrap.user.id);
      const subject = item.kind === "expense" ? (item.comment || "Затрата") : `Возврат → ${item.counterparty_name}`;
      return `<article class="history-row"><div class="row-between"><div><div class="row-title">${item.kind === "expense" ? "💸" : "🤝"} ${e(subject)}</div><div class="row-note">${e(item.creator_name)} · ${shortDate(item.created_at)}</div></div><div class="amount">${money(item.amount, collection.currency)}</div></div><div class="row-note">${item.kind === "expense" ? `На: ${e(item.shared_with || "—")}` : "Фактический перевод"} ${item.status === "cancelled" ? '<span class="pill cancelled">отменено</span>' : ""}</div>${canEdit ? `<div class="transaction-actions"><button class="mini-button" type="button" data-action="edit-transaction" data-id="${item.id}">Изменить</button><button class="mini-button danger" type="button" data-action="cancel-transaction" data-id="${item.id}">Отменить</button></div>` : ""}</article>`;
    }).join("")}</div>`;
  }
  if (state.collectionTab === "members") {
    return `<div class="card">${data.participants.map((member) => `<div class="member-row"><div class="row-between"><div><div class="row-title">${e(member.full_name)} ${member.is_admin ? "👑" : ""}</div><div class="row-note">${member.username ? `@${e(member.username)}` : `ID ${member.id}`}${member.payment_details ? `<br>💳 ${e(member.payment_details)}` : ""}</div></div></div></div>`).join("")}</div>${collection.status === "active" && !isAdmin ? '<div class="sheet-actions"><button class="danger-button" type="button" data-action="leave">Выйти из сбора</button></div>' : ""}`;
  }
  return `<div class="card member-row"><div class="row-title">Администратор сбора</div><div class="row-note">Передача роли и удаление участников доступны только при соблюдении балансов.</div></div><div class="sheet-actions">${collection.status === "active" ? '<button class="secondary-button" type="button" data-action="transfer">Передать администратора</button><button class="secondary-button" type="button" data-action="remove-member">Удалить участника</button><button class="danger-button" type="button" data-action="archive">Завершить и архивировать</button>' : '<button class="primary-button" type="button" data-action="restore">Восстановить сбор</button>'}</div>`;
}

function createSheet() {
  const chats = state.bootstrap.chats;
  showSheet(`<h2>Новый сбор</h2><p class="sheet-intro">Выберите Telegram-группу или добавьте новую — бот должен уже состоять в ней.</p>${chats.length ? `<form id="create-form"><label class="field"><span>Группа</span><select name="chat_id" required>${chats.map((chat) => `<option value="${chat.chat_id}">${e(chat.label)}</option>`).join("")}</select></label><label class="field"><span>Название</span><input name="title" minlength="2" maxlength="80" placeholder="Например, Поездка в Варшаву" required></label><label class="field"><span>Валюта</span><select name="currency">${state.bootstrap.currencies.map((currency) => `<option>${currency}</option>`).join("")}</select></label><div class="sheet-actions"><button class="primary-button" type="submit">Создать сбор</button><button class="secondary-button" type="button" data-action="choose-group">Выбрать другую группу</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>` : `${empty("👥", "Выберите группу, в которую уже добавлен бот.")}<div class="sheet-actions"><button class="primary-button" type="button" data-action="choose-group">Выбрать Telegram-группу</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div>`}`);
}

function expenseSheet() {
  const data = state.collection;
  showSheet(`<h2>Добавить затрату</h2><p class="sheet-intro">Сумма будет поровну распределена между отмеченными людьми.</p><form id="expense-form"><label class="field"><span>Сумма · ${e(data.collection.currency)}</span><input name="amount" inputmode="decimal" placeholder="0,00" required></label><label class="field"><span>Комментарий</span><input name="comment" maxlength="200" placeholder="Например, билеты"></label><div class="row-between"><span class="row-title">На кого делим</span><button class="text-button" type="button" data-action="select-all">Выбрать всех</button></div><div class="check-list">${data.participants.map((member) => `<label class="check-row"><input type="checkbox" name="participant" value="${member.id}"><span>${e(member.full_name)}${member.id === state.bootstrap.user.id ? " · вы" : ""}</span></label>`).join("")}</div><div class="sheet-actions"><button class="primary-button" type="submit">Добавить затрату</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

function repaySheet() {
  const data = state.collection;
  const debts = data.debts.filter((debt) => debt.debtor_id === state.bootstrap.user.id);
  if (!debts.length) {
    toast("По текущему балансу у вас нет долгов");
    return;
  }
  showSheet(`<h2>Вернуть долг</h2><p class="sheet-intro">Запишите только уже выполненный перевод.</p><form id="repay-form"><label class="field"><span>Получатель</span><select name="creditor_id">${debts.map((debt) => `<option value="${debt.creditor_id}">${e(debt.creditor_name)} · до ${money(debt.amount, data.collection.currency)}</option>`).join("")}</select></label><label class="field"><span>Переведено · ${e(data.collection.currency)}</span><input name="amount" inputmode="decimal" placeholder="0,00" required></label><label class="field"><span>Комментарий</span><input name="comment" maxlength="200" placeholder="Необязательно"></label><div class="sheet-actions"><button class="primary-button" type="submit">Записать возврат</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

function editSheet(transactionId) {
  const item = state.collection.history.find((row) => row.id === Number(transactionId));
  showSheet(`<h2>Изменить транзакцию</h2><p class="sheet-intro">Распределение участников сохранится, балансы пересчитаются.</p><form id="edit-form" data-id="${item.id}"><label class="field"><span>Сумма · ${e(state.collection.collection.currency)}</span><input name="amount" inputmode="decimal" value="${(item.amount / 100).toFixed(2).replace(".", ",")}" required></label><label class="field"><span>Комментарий</span><input name="comment" maxlength="200" value="${e(item.comment)}"></label><div class="sheet-actions"><button class="primary-button" type="submit">Сохранить</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

function paymentSheet() {
  showSheet(`<h2>Платежные данные</h2><p class="sheet-intro">Например, телефон СБП или последние цифры карты. Не указывайте секретные коды.</p><form id="payment-form"><label class="field"><span>Реквизиты</span><textarea name="payment_details" maxlength="500" placeholder="Можно оставить пустым">${e(state.bootstrap.user.payment_details)}</textarea></label><div class="sheet-actions"><button class="primary-button" type="submit">Сохранить</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

function memberActionSheet(type) {
  const candidates = state.collection.participants.filter((member) => member.id !== state.bootstrap.user.id);
  const titleText = type === "transfer" ? "Передать администратора" : "Удалить участника";
  showSheet(`<h2>${titleText}</h2><p class="sheet-intro">${type === "transfer" ? "Новый администратор получит все права управления сбором." : "Удалить можно участника с нулевым балансом."}</p><form id="member-action-form" data-type="${type}"><label class="field"><span>Участник</span><select name="user_id">${candidates.map((member) => `<option value="${member.id}">${e(member.full_name)}</option>`).join("")}</select></label><div class="sheet-actions"><button class="${type === "transfer" ? "primary-button" : "danger-button"}" type="submit">${titleText}</button><button class="secondary-button" type="button" data-action="close-sheet">Отмена</button></div></form>`);
}

function confirmAction(message) {
  return new Promise((resolve) => {
    if (tg?.showConfirm) tg.showConfirm(message, resolve);
    else resolve(window.confirm(message));
  });
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
    if (action === "create") return createSheet();
    if (action === "expense") return expenseSheet();
    if (action === "repay") return repaySheet();
    if (action === "tab") { state.collectionTab = target.dataset.tab; renderCollection(); return; }
    if (action === "edit-transaction") return editSheet(target.dataset.id);
    if (action === "payment") return paymentSheet();
    if (action === "transfer" || action === "remove-member") return memberActionSheet(action === "transfer" ? "transfer" : "remove");
    if (action === "cancel-transaction") {
      if (!await confirmAction("Отменить транзакцию? Она останется в истории.")) return;
      setBusy(target, true);
      const result = await api(`/api/transactions/${target.dataset.id}/cancel`, { method: "POST", body: "{}" });
      reportToast(result, "Транзакция отменена");
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

sheet.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-action]");
  if (!target || state.busy) return;
  if (target.dataset.action === "close-sheet") closeSheet();
  if (target.dataset.action === "select-all") {
    sheet.querySelectorAll('input[name="participant"]').forEach((input) => { input.checked = true; });
    haptic();
  }
  if (target.dataset.action === "choose-group") {
    if (!tg?.isVersionAtLeast?.("9.6") || !tg?.requestChat) {
      toast("Обновите Telegram или создайте первый сбор командой /new", true);
      return;
    }
    try {
      setBusy(target, true);
      const prepared = await api("/api/chats/prepare", { method: "POST", body: "{}" });
      tg.requestChat(prepared.request_id, async (success) => {
        setBusy(target, false);
        if (!success) return;
        closeSheet();
        toast("Группа добавляется…");
        await new Promise((resolve) => setTimeout(resolve, 900));
        await reloadBootstrap();
        createSheet();
      });
    } catch (error) {
      setBusy(target, false);
      toast(error.message, true);
    }
  }
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
      result = await api("/api/collections", { method: "POST", body: JSON.stringify({ chat_id: values.get("chat_id"), title: values.get("title"), currency: values.get("currency") }) });
      closeSheet();
      reportToast(result, "Сбор создан");
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
      result = await api(`/api/transactions/${form.dataset.id}`, { method: "PATCH", body: JSON.stringify({ amount: values.get("amount"), comment: values.get("comment") }) });
      closeSheet(); reportToast(result, "Транзакция обновлена"); return await refreshCurrent("history");
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
    if (button.dataset.nav === "profile") renderProfile();
  } catch (error) { toast(error.message, true); }
});

avatar.addEventListener("click", renderProfile);
document.getElementById("sheet-backdrop").addEventListener("click", closeSheet);
tg?.BackButton?.onClick(() => state.collection ? renderCollections() : tg.close());

async function init() {
  tg?.ready();
  tg?.expand();
  tg?.setHeaderColor?.("bg_color");
  tg?.setBackgroundColor?.("bg_color");
  if (!tg?.initData) {
    nav.hidden = true;
    app.innerHTML = `<section class="auth-error"><span class="empty-icon">🔐</span><h2>Откройте из Telegram</h2><p class="row-note">Так мы безопасно узнаем ваш Telegram ID без логина и пароля.</p></section>`;
    return;
  }
  try {
    await reloadBootstrap();
    renderCollections();
  } catch (error) {
    nav.hidden = true;
    app.innerHTML = `<section class="auth-error"><span class="empty-icon">↻</span><h2>Не удалось открыть приложение</h2><p class="row-note">${e(error.message)}</p><div class="sheet-actions"><button class="primary-button" type="button" id="retry">Попробовать снова</button></div></section>`;
    document.getElementById("retry")?.addEventListener("click", () => window.location.reload());
  }
}

init();
