"use strict";

const tg = window.Telegram?.WebApp;
const auth = tg?.initData || "";
const byId = id => document.getElementById(id);
const notices = byId("notices");
let state;
let selectedTab = "search";
let selectedFilter = "all";
let pendingSuperjob = "";
let hhLoginUrl = "";
let loadingHHResumes = false;
let profileDirty = false;
let searches = [];

const modes = {
  browser: "Поиск и отклики",
  account: "Поиск и отклики",
  public: "Только поиск",
  key: "Поиск подключён",
  needs_key: "Требуется настройка",
  link: "Нет подключения",
};
const sent = new Set(["sent", "reported", "duplicate"]);

function el(tag, value, css) {
  const node = document.createElement(tag);
  if (value) node.textContent = value;
  if (css) node.className = css;
  return node;
}

function showError(message = "") {
  byId("error").textContent = message;
  byId("error").hidden = !message;
}

function showFeedback(message = "") {
  byId("feedback").textContent = message;
  byId("feedback").hidden = !message;
}

function external(url) {
  const target = new URL(url);
  if (target.protocol !== "https:") return;
  if (tg?.openLink) tg.openLink(target.href);
  else window.open(target.href, "_blank", "noopener,noreferrer");
}

async function api(command, payload) {
  const response = await fetch("/mini/api/" + command, {
    method: payload === undefined ? "GET" : "POST",
    headers: { Authorization: "tma " + auth, "Content-Type": "application/json" },
    body: payload === undefined ? undefined : JSON.stringify(payload),
    cache: "no-store",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "Сервер не ответил. Попробуйте ещё раз.");
  return data;
}

function action(label, fn, primary = false) {
  const button = el("button", label, primary ? "primary" : "secondary");
  button.type = "button";
  button.addEventListener("click", async () => {
    showError();
    showFeedback();
    button.disabled = true;
    try {
      await fn();
    } catch (error) {
      showError(error.message);
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

function confirmAction(message) {
  if (tg?.showConfirm && tg.isVersionAtLeast?.("6.2")) {
    return new Promise(resolve => tg.showConfirm(message, resolve));
  }
  return Promise.resolve(window.confirm(message));
}

function changeTab(tab) {
  showError();
  showFeedback();
  selectedTab = tab;
  for (const button of document.querySelectorAll("#navigation button")) {
    const active = button.dataset.tab === tab;
    byId("panel-" + button.dataset.tab).hidden = !active;
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  window.scrollTo(0, 0);
  if (tab !== "search") refresh().catch(error => showError(error.message));
}

function sourceName(key) {
  return state.sources.find(source => source.id === key)?.name || "Ссылка";
}

function statusClass(status) {
  if (status === "sent" || status === "duplicate") return "status good";
  return "status" + (["unknown", "error", "questions"].includes(status) ? " attention" : "");
}

function canSend(job) {
  if (job.source === "hh") return state.hh_send && state.hh_connected && state.hh_resume && state.consent && state.groq;
  if (job.source === "superjob") return state.superjob.send && state.superjob.connected && state.superjob.selected;
  return false;
}

async function showVacancy(job) {
  const detail = await api("vacancy?" + new URLSearchParams({ id: job.id }));
  byId("job-dialog-title").textContent = detail.title;
  const body = byId("job-description-body");
  body.replaceChildren(
    el("p", [detail.company, detail.region, detail.salary].filter(Boolean).join(" / "), "muted"),
    el("p", detail.description || "В карточке нет подробного описания.")
  );
  if (job.url) body.append(action("Карточка на сайте", () => external(job.url)));
  byId("job-dialog").showModal();
}

function jobCard(job, history) {
  const card = el("article", "", "card");
  card.dataset.jobId = job.id;
  const header = el("div", "", "card-header");
  header.append(el("span", sourceName(job.source), "chip"));
  if (job.label) header.append(el("span", job.label, statusClass(job.status)));
  card.append(header, el("h3", job.title), el("p", [job.company, job.region].filter(Boolean).join(" · "), "muted"),
    el("p", job.salary || "Зарплата не указана", "salary"));
  if (Number.isFinite(job.score)) card.append(el("p", "Соответствие резюме " + job.score + "%", "muted"));
  if (job.reason) card.append(el("p", job.reason, "muted"));
  if (job.letter) {
    const letter = el("details");
    letter.append(el("summary", "Сопроводительное письмо"), el("p", job.letter, "letter"));
    card.append(letter);
    if (history && job.can_edit) {
      const edit = el("details", "", "letter-editor");
      const field = document.createElement("textarea");
      field.rows = 4;
      field.maxLength = 1200;
      field.value = job.letter;
      field.setAttribute("aria-label", "Текст сопроводительного письма");
      edit.append(el("summary", "Редактировать письмо"), field, action("Сохранить письмо", async () => {
        await api("job", { id: job.id, action: "edit", letter: field.value });
        await refresh();
      }));
      letter.append(edit);
    }
  }
  const actions = el("div", "", "actions");
  const provider = state.sources.find(source => source.id === job.source);
  if (!history) {
    actions.append(action(job.saved ? "К откликам" : provider?.in_app_apply ? "Подготовить отклик" : "Сохранить вакансию", async () => {
      if (!job.saved) {
        await api("job", { id: job.id, action: "prepare" });
        job.saved = true;
        renderSearch();
      }
      changeTab("jobs");
    }, true));
    if (!provider?.in_app_apply) card.append(el("p", "Площадка доступна для поиска. Отправка здесь не подключена.", "muted"));
  } else if (job.can_send && canSend(job)) {
    const button = action("Отправить отклик", async () => {
      if (await confirmAction("Отправить резюме и письмо работодателю через " + sourceName(job.source) + "?")) {
        button.textContent = "Отправляю…";
        try {
          const result = await api("job", { id: job.id, action: "send", confirm: true });
          await refresh();
          if (result.job.status === "sent" || result.job.status === "duplicate") showFeedback(result.job.label);
          if (result.job.status === "unknown") showError("Площадка пока не подтвердила отправку. Статус сохранён в журнале.");
        } catch (error) {
          await refresh().catch(() => {});
          throw error;
        } finally {
          button.textContent = "Отправить отклик";
        }
      }
    }, true);
    actions.append(button);
  } else if (job.can_send && provider?.in_app_apply) {
    actions.append(action("Проверить аккаунт", () => changeTab("connections")));
  }
  if (history && job.status === "unknown" && job.source === "hh") {
    actions.append(action("Проверить отправку", async () => {
      await api("job", { id: job.id, action: "reconcile" });
      await refresh();
      showFeedback("Отправка подтверждена площадкой.");
    }));
  }
  if (history && job.status === "unknown" && job.source === "superjob") {
    card.append(el("p", "SuperJob не подтвердил результат. Повторная отправка заблокирована.", "muted"));
  }
  actions.append(action("Описание", () => showVacancy(job)));
  if (job.letter && navigator.clipboard) actions.append(action("Скопировать письмо", async () => {
    await navigator.clipboard.writeText(job.letter);
    showFeedback("Письмо скопировано.");
  }));
  card.append(actions);
  return card;
}

function renderSearch() {
  byId("results").replaceChildren(...searches.map(job => jobCard(job, false)));
}

function renderJobs() {
  const filtered = state.jobs.filter(job => selectedFilter === "all" || (selectedFilter === "sent") === sent.has(job.status));
  byId("total-count").textContent = state.summary.total;
  byId("sent-count").textContent = state.summary.sent;
  byId("today-count").textContent = state.summary.today + " / " + state.daily_limit;
  const setup = byId("jobs-setup");
  setup.hidden = !state.jobs.some(job => job.status === "queued") || Boolean(state.resume && state.consent && state.groq);
  setup.replaceChildren(
    el("span", "Для писем нужен профиль и согласие на анализ. "),
    action("Открыть профиль", () => changeTab("connections"))
  );
  if (!filtered.length) {
    byId("jobs").replaceChildren(el("p", selectedFilter === "sent" ? "Отправленных откликов пока нет." : "Подготовьте отклик из результатов поиска.", "empty"));
    return;
  }
  byId("jobs").replaceChildren(...filtered.map(job => jobCard(job, true)));
}

function resumePicker(label, resumes, active, use) {
  const group = el("div", "", "stack");
  const chosen = document.createElement("select");
  chosen.setAttribute("aria-label", label);
  for (const [id, title] of Object.entries(resumes)) chosen.append(new Option(title, id));
  if (active) chosen.value = active;
  group.append(chosen, action("Использовать резюме", () => use(chosen.value)));
  return group;
}

function showHHLogin() {
  byId("hh-frame").src = hhLoginUrl;
  byId("hh-dialog").showModal();
}

async function openHH(fresh = false) {
  hhLoginUrl = (await api("connect", { fresh })).url;
  showHHLogin();
  await refresh();
}

async function loadHHResumes() {
  if (loadingHHResumes) return;
  loadingHHResumes = true;
  showFeedback("Загружаю резюме из HH…");
  try {
    await api("hh-resumes", {});
  } finally {
    loadingHHResumes = false;
    showFeedback();
    await refresh().catch(() => {});
  }
}

function renderHH(card, operations) {
  const connected = state.hh_connected;
  const hasResume = connected && state.hh_resume;
  if (hasResume) card.append(el("p", state.hh_resume, "resume-name"));
  if (connected && !hasResume) {
    card.append(el("p", state.hh_resumes_status || "Можно искать вакансии. Для откликов выберите резюме HH.", "muted"));
  } else if (connected && state.hh_resumes_status) {
    card.append(el("p", state.hh_resumes_status, "muted"));
  }
  if (!connected && state.hh_progress) card.append(el("p", state.hh_progress, "muted"));
  if (state.hh_running) {
    card.append(el("p", state.hh_auto ? "Автоотклики включены" : "Фоновый поиск включён", "muted"));
    if (!state.hh_login_open) operations.append(action("Найденные вакансии", () => changeTab("jobs")));
  }
  if (state.hh_notice && connected && !state.hh_login_open) {
    card.append(el("p", state.hh_notice, "muted"));
  }
  if (state.hh_login_open) {
    if (!byId("hh-dialog").open) {
      if (hhLoginUrl) operations.append(action("Продолжить вход", showHHLogin, true));
      operations.append(action("Отменить вход", async () => {
        await api("cancel", {});
        hhLoginUrl = "";
        await refresh();
      }));
    }
    return;
  }
  if (!connected) operations.append(action("Подключить HH", () => openHH(), true));
  if (connected && !state.hh_running) {
    operations.append(action("Включить фоновый поиск", async () => {
      await api("hh-mode", { mode: "search" });
      await refresh();
      showFeedback("Фоновый поиск HH включён.");
    }, true));
  }
  if (connected) {
    const resumeButton = action(hasResume ? "Обновить резюме" : "Загрузить резюме", loadHHResumes);
    resumeButton.disabled = loadingHHResumes;
    operations.append(resumeButton);
  }
  if (connected || state.hh_has_session) {
    operations.append(action("Сменить аккаунт", () => openHH(true)));
    operations.append(action("Отключить", async () => {
      if (await confirmAction("Закрыть сохранённую сессию HH?")) {
        await api("disconnect", { confirm: true });
        hhLoginUrl = "";
        await refresh();
      }
    }));
  }
  if (connected && Object.keys(state.resumes).length && (!hasResume || Object.keys(state.resumes).length > 1)) {
    card.append(resumePicker("Выберите резюме HH", state.resumes, state.hh_resume_id, async id => {
      await api("resume", { id });
      profileDirty = false;
      await refresh();
    }));
  }
  if (hasResume && state.consent && state.groq) {
    if (state.hh_auto_allowed && !state.hh_auto) operations.append(action("Включить автоотклики", async () => {
      const message = "Разрешить отправлять резюме и письма работодателям HH? Лимит на день: " + state.daily_limit;
      if (await confirmAction(message)) {
        await api("hh-mode", { mode: "auto", confirm: true });
        await refresh();
      }
    }));
  } else if (hasResume && !state.consent) {
    operations.append(action("Настроить письма", () => byId("profile-form").scrollIntoView({ block: "start" })));
  }
  if (state.hh_running) operations.append(action("Остановить поиск", async () => {
    await api("hh-mode", { mode: "stop" });
    await refresh();
  }));
}

function renderSuperjob(card, operations) {
  const account = state.superjob;
  if (!account.enabled) {
    card.append(el("p", "Для входа нужен зарегистрированный ключ и ID приложения SuperJob.", "muted"));
    return;
  }
  if (account.status) card.append(el("p", account.status, "muted"));
  if (pendingSuperjob && !account.status.startsWith("Авторизация открыта")) pendingSuperjob = "";
  if (pendingSuperjob) operations.append(action("Войти на SuperJob", () => external(pendingSuperjob), true));
  operations.append(action(account.connected ? "Подключить заново" : pendingSuperjob ? "Создать новую ссылку" : "Подключить SuperJob", async () => {
    pendingSuperjob = (await api("superjob", { action: "connect" })).url;
    await refresh();
  }, !pendingSuperjob));
  if (account.connected) {
    if (account.selected) card.append(el("p", account.resumes[account.selected], "resume-name"));
    if (Object.keys(account.resumes).length && (!account.selected || Object.keys(account.resumes).length > 1)) {
      card.append(resumePicker("Выберите резюме SuperJob", account.resumes, account.selected, async id => {
        await api("superjob", { action: "resume", id });
        await refresh();
      }));
    }
    operations.append(action("Отключить", async () => {
      if (await confirmAction("Отключить аккаунт SuperJob?")) {
        await api("superjob", { action: "disconnect", confirm: true });
        pendingSuperjob = "";
        await refresh();
      }
    }));
  }
}

function renderConnections() {
  const cards = [];
  for (const source of state.sources) {
    const card = el("article", "", "card");
    const head = el("div", "", "card-header");
    const linked = (source.id === "hh" && state.hh_connected) ||
      (source.id === "superjob" && state.superjob.connected && !state.superjob.status.startsWith("Нужно снова"));
    head.append(el("h3", source.name), el("span", linked ? "Подключён" : modes[source.mode], linked ? "status good" : "status"));
    card.append(head);
    const operations = el("div", "", "actions");
    if (source.id === "hh" && source.mode === "browser") renderHH(card, operations);
    else if (source.id === "superjob") renderSuperjob(card, operations);
    else card.append(el("p", source.mode === "public" ? "Вакансии доступны в поиске. Личный аккаунт здесь не подключается." :
      "Подключение аккаунта и отправка откликов пока недоступны.", "muted"));
    if (operations.childElementCount) card.append(operations);
    cards.push(card);
  }
  byId("connections").replaceChildren(...cards);
  byId("profile-info").textContent = [
    state.hh_connected && state.hh_resume ? "Резюме HH уже в профиле." :
      state.resume ? "Резюме сохранено." :
      state.hh_connected ? "Загрузите резюме HH для подготовки писем." : "Подключите HH, чтобы загрузить резюме из аккаунта.",
    !state.groq ? "Для писем нужно настроить Groq." : "",
    !state.persistent ? "Для отправки подключите постоянное хранилище Bothost." : "",
  ].filter(Boolean).join(" ");
  if (!profileDirty) {
    byId("resume").value = state.resume;
    byId("consent").checked = state.consent;
  }
}

async function refresh(first = false, unchanged = false) {
  const next = await api("state");
  if (unchanged && JSON.stringify(state) === JSON.stringify(next)) return;
  state = next;
  if (!state.hh_login_open && !byId("hh-dialog").open) hhLoginUrl = "";
  const version = byId("app-version");
  version.hidden = !state.version;
  version.textContent = state.version ? "Версия " + state.version : "";
  if (first) {
    for (const key of ["query", "region", "salary"]) byId(key).value = state.defaults[key];
    for (const source of state.sources) byId("source").append(new Option(source.name, source.id));
    byId("launch").hidden = true;
    byId("navigation").hidden = byId("panel-search").hidden = false;
  }
  if (selectedTab === "jobs") renderJobs();
  if (selectedTab === "connections") renderConnections();
}

byId("search-form").addEventListener("submit", async event => {
  event.preventDefault();
  showError();
  const submit = byId("search-submit");
  submit.disabled = true;
  submit.textContent = "Ищу…";
  byId("search-status").textContent = "Ищу по выбранным источникам…";
  byId("search-status").hidden = false;
  notices.replaceChildren();
  byId("result-count").textContent = "";
  try {
    const query = new URLSearchParams(new FormData(event.target));
    if (!query.has("hh_title_only")) query.set("hh_title_only", "false");
    const result = await api("search?" + query);
    searches = result.jobs;
    renderSearch();
    byId("result-count").textContent = "Найдено: " + searches.length;
    byId("search-status").hidden = Boolean(searches.length);
    if (!searches.length) byId("search-status").textContent = "Вакансии не найдены. Попробуйте изменить параметры поиска.";
    for (const notice of result.notices) {
      const node = el("div", "", "source-note");
      node.append(el("p", sourceName(notice.source) + " · " + notice.message, "muted"));
      node.append(action("Аккаунты", () => changeTab("connections")));
      notices.append(node);
    }
  } catch (error) {
    showError(error.message);
    byId("search-status").textContent = "Выполните поиск повторно.";
  } finally {
    submit.disabled = false;
    submit.textContent = "Найти";
  }
});

byId("import-form").addEventListener("submit", async event => {
  event.preventDefault();
  showError();
  const button = event.submitter;
  if (button) button.disabled = true;
  try {
    const result = await api("import", Object.fromEntries(new FormData(event.target)));
    event.target.reset();
    event.target.parentElement.open = false;
    selectedFilter = "all";
    for (const item of document.querySelectorAll("#jobs-filters button")) {
      item.setAttribute("aria-pressed", String(item.dataset.filter === "all"));
    }
    await refresh();
    const card = Array.from(byId("jobs").children).find(item =>
      item.dataset.jobId === result.job.id);
    card?.scrollIntoView({ block: "center" });
  } catch (error) {
    showError(error.message);
  } finally {
    if (button) button.disabled = false;
  }
});

byId("profile-form").addEventListener("input", () => { profileDirty = true; });
byId("profile-form").addEventListener("submit", async event => {
  event.preventDefault();
  showError();
  const button = event.submitter;
  if (button) button.disabled = true;
  try {
    await api("profile", { resume: byId("resume").value, consent: byId("consent").checked });
    profileDirty = false;
    await refresh();
    byId("profile-info").textContent = "Профиль сохранён.";
  } catch (error) {
    showError(error.message);
  } finally {
    if (button) button.disabled = false;
  }
});

for (const button of document.querySelectorAll("#navigation button")) {
  button.addEventListener("click", () => changeTab(button.dataset.tab));
}
for (const button of document.querySelectorAll("#jobs-filters button")) {
  button.addEventListener("click", () => {
    selectedFilter = button.dataset.filter;
    for (const item of document.querySelectorAll("#jobs-filters button")) {
      item.setAttribute("aria-pressed", String(item === button));
    }
    renderJobs();
  });
}
byId("reload").addEventListener("click", () => {
  if (auth) refresh(!state).catch(error => showError(error.message));
});
const hhDialog = byId("hh-dialog");
const hhFrame = byId("hh-frame");
byId("close-hh").addEventListener("click", () => hhDialog.close());
byId("close-job").addEventListener("click", () => byId("job-dialog").close());
hhDialog.addEventListener("close", async () => {
  hhFrame.removeAttribute("src");
  try {
    await refresh();
  } catch (error) {
    showError(error.message);
  }
});
window.addEventListener("message", event => {
  if (event.origin !== location.origin || event.source !== hhFrame.contentWindow) return;
  if (event.data?.hhLogin === "done") {
    hhLoginUrl = "";
    hhDialog.close();
    showFeedback("Вход в HH подтверждён. Сессия сохранена.");
  } else if (["blocked", "failed", "expired"].includes(event.data?.hhLogin)) {
    hhLoginUrl = "";
    refresh(false, true).catch(() => {});
  }
});
document.addEventListener("visibilitychange", () => {
  if (state && document.visibilityState === "visible" && selectedTab === "connections") {
    refresh(false, true).catch(() => {});
  }
});
if (auth) {
  tg.ready();
  tg.expand?.();
  refresh(true).catch(error => { byId("launch").textContent = error.message; });
  window.setInterval(() => {
    if (state && document.visibilityState === "visible" && selectedTab !== "search") {
      if (document.querySelector(".letter-editor[open]")) return;
      refresh(false, true).catch(() => {});
    }
  }, 6000);
} else {
  byId("launch").textContent = "Откройте «Поиск работы» из личного чата с Telegram-ботом.";
}
