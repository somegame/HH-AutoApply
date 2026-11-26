"use strict";

const tg = window.Telegram?.WebApp;
const auth = tg?.initData || "";
const byId = id => document.getElementById(id);
const notices = byId("notices");
let state;
let selectedTab = "search";
let selectedFilter = "all";
let pendingLink = "";
let profileDirty = false;
let searches = [];

const modes = {
  browser: "Поиск в приложении",
  public: "Открытый каталог",
  key: "Поиск по API",
  needs_key: "Для поиска здесь нужен ключ",
  link: "Поиск на сайте",
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

function jobCard(job, history) {
  const card = el("article", "", "card");
  const header = el("div", "", "card-header");
  header.append(el("span", sourceName(job.source), "chip"));
  if (job.label) header.append(el("span", job.label, "muted"));
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
  if (!history) {
    actions.append(action(job.saved ? "К откликам" : "Подготовить отклик", async () => {
      if (!job.saved) {
        await api("job", { id: job.id, action: "prepare" });
        job.saved = true;
        renderSearch();
      }
      changeTab("jobs");
    }, true));
  } else if (job.can_send && state.hh_send && state.hh_resume) {
    actions.append(action("Отправить через HH", async () => {
      if (await confirmAction("Отправить это резюме и сопроводительное работодателю?")) {
        await api("job", { id: job.id, action: "send", confirm: true });
        await refresh();
      }
    }, true));
  }
  if (history && job.status === "unknown" && job.source === "hh") {
    actions.append(action("Проверить отправку", async () => {
      await api("job", { id: job.id, action: "reconcile" });
      await refresh();
    }));
  }
  if (job.url) {
    const title = history && job.status === "unknown" ? "Проверить на сайте" :
      history && !sent.has(job.status) ? "Открыть для отклика" : "На сайте";
    actions.append(action(title, () => external(job.url)));
  }
  if (history && !sent.has(job.status) && !["sending", "unknown", "rejected"].includes(job.status)) {
    actions.append(action("Отметить отправленным", async () => {
      if (await confirmAction("Вы уже отправили отклик на сайте?")) {
        await api("job", { id: job.id, action: "reported", confirm: true });
        await refresh();
      }
    }));
  }
  if (job.letter && navigator.clipboard) actions.append(action("Скопировать письмо", async () => {
    await navigator.clipboard.writeText(job.letter);
  }));
  card.append(actions);
  return card;
}

function renderSearch() {
  byId("results").replaceChildren(...searches.map(job => jobCard(job, false)));
}

function renderJobs() {
  const filtered = state.jobs.filter(job => selectedFilter === "all" || (selectedFilter === "sent") === sent.has(job.status));
  byId("jobs-count").textContent = filtered.length ? String(filtered.length) : "";
  if (!filtered.length) {
    byId("jobs").replaceChildren(el("p", selectedFilter === "sent" ? "Отправленных откликов пока нет." : "Подготовьте отклик из результатов поиска.", "notice muted"));
    return;
  }
  byId("jobs").replaceChildren(...filtered.map(job => jobCard(job, true)));
}

function renderConnections() {
  const cards = [];
  for (const source of state.sources) {
    const card = el("article", "", "card");
    card.append(el("h3", source.name), el("p", modes[source.mode], "muted"));
    const operations = el("div", "", "actions");
    operations.append(action("Открыть сайт", () => external(source.url)));
    if (source.id === "hh" && source.mode === "browser") {
      const note = state.hh_resume ? "Выбрано резюме " + state.hh_resume : state.hh_progress;
      if (note) card.append(el("p", note, "muted"));
      if (state.hh_running) {
        card.append(el("p", state.hh_auto ? "Автоотклики включены" : "Фоновый поиск включён", "muted"));
      }
      if (state.hh_notice) card.append(el("p", state.hh_notice, "muted"));
      if (pendingLink && state.hh_progress !== "Окно входа открыто.") pendingLink = "";
      operations.append(pendingLink
        ? action("Открыть вход HH", () => external(pendingLink), true)
        : action("Подключить HH", async () => {
            pendingLink = (await api("connect", {})).url;
            await refresh();
          }, true)
      );
      if (state.hh_has_session && !pendingLink) {
        operations.append(action("Отключить HH", async () => {
          if (await confirmAction("Закрыть сохранённую сессию HH на Bothost?")) {
            await api("disconnect", { confirm: true });
            await refresh();
          }
        }));
      }
      if (pendingLink || state.hh_progress === "Окно входа открыто.") {
        operations.append(action("Закрыть окно входа", async () => {
          await api("cancel", {});
          pendingLink = "";
          await refresh();
        }));
      }
      if (Object.keys(state.resumes).length) {
        const chosen = document.createElement("select");
        chosen.setAttribute("aria-label", "Выберите резюме HH");
        for (const [id, title] of Object.entries(state.resumes)) chosen.append(new Option(title, id));
        card.append(chosen, action("Использовать резюме", async () => {
          await api("resume", { id: chosen.value });
          profileDirty = false;
          await refresh();
          byId("profile-form").scrollIntoView({ block: "start" });
        }));
      }
      if (state.hh_has_session && state.hh_resume && state.consent && state.groq) {
        if (!state.hh_running) operations.append(action("Включить фоновый поиск", async () => {
          await api("hh-mode", { mode: "search" });
          await refresh();
        }));
        if (state.hh_send && !state.hh_auto) operations.append(action("Включить автоотклики", async () => {
          const message = "Разрешить отправлять резюме и письма работодателям HH? Лимит на день: " + state.daily_limit;
          if (await confirmAction(message)) {
            await api("hh-mode", { mode: "auto", confirm: true });
            await refresh();
          }
        }));
      }
      if (state.hh_running) operations.append(action("Остановить поиск и автоотклики", async () => {
        await api("hh-mode", { mode: "stop" });
        await refresh();
      }));
    } else if (source.id === "superjob" && source.mode === "needs_key") {
      card.append(el("p", "Для поиска через API укажите SUPERJOB_API_KEY на хостинге.", "muted"));
    }
    card.append(operations);
    cards.push(card);
  }
  byId("connections").replaceChildren(...cards);
  byId("profile-info").textContent = [
    state.groq ? "Groq доступен при согласии на анализ." : "Для анализа требуется ключ Groq.",
    state.persistent ? "" : "Постоянное хранилище не подтверждено.",
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
    byId("result-count").textContent = String(searches.length);
    byId("search-status").hidden = Boolean(searches.length);
    if (!searches.length) byId("search-status").textContent = "По этим параметрам вакансии пока не найдены. Можно открыть поиск на источнике.";
    for (const notice of result.notices) {
      const node = el("div", "", "source-note");
      node.append(el("p", sourceName(notice.source) + " · " + notice.message, "muted"));
      const source = result.sources.find(item => item.id === notice.source);
      if (source) node.append(action("Искать на сайте", () => external(source.url)));
      notices.append(node);
    }
  } catch (error) {
    showError(error.message);
    byId("search-status").textContent = "Выполните поиск повторно.";
  } finally {
    submit.disabled = false;
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
      item.querySelector("h3")?.textContent === result.job.title);
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
if (auth) {
  tg.ready();
  tg.expand?.();
  refresh(true).catch(error => { byId("launch").textContent = error.message; });
  window.setInterval(() => {
    if (state && document.visibilityState === "visible" && selectedTab !== "search") {
      if (document.querySelector(".letter-editor[open]")) return;
      if (selectedTab === "connections" && !pendingLink && state.hh_progress !== "Окно входа открыто.") return;
      refresh(false, true).catch(() => {});
    }
  }, 6000);
} else {
  byId("launch").textContent = "Откройте «Поиск работы» из личного чата с Telegram-ботом.";
}
