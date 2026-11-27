"use strict";

const tg = window.Telegram?.WebApp;
const auth = tg?.initData || "";
const byId = id => document.getElementById(id);
const notices = byId("notices");
const hhDialog = byId("hh-dialog");
const hhFrame = byId("hh-frame");
const hhResumeUrlDialog = byId("hh-resume-url-dialog");
const hhResumeUrlField = byId("hh-resume-url");
const hhResumeBoundMessage = "Резюме HH загружено и привязано к профилю.";
let state;
let selectedTab = "search";
let selectedFilter = "all";
let journalSource = "all";
let pendingSuperjob = "";
let pendingHabr = "";
let hhWindow;
let loadingHHResumes = false;
let profileDirty = false;
let searches = [];
let activeSearch;
let lastSearchQuery;
let searchTimer;
let startingSearch = false;
let skillsDirty = false;
let hhEditorTimer;
const resumeStudio = {
  initialized: false,
  starting: false,
  adopting: false,
  checking: false,
  timer: undefined,
  pollError: "",
  formError: "",
};

const modes = {
  browser: "Поиск и отклики",
  account: "Поиск и отклики",
  public: "Только поиск",
  habr: "Подтверждение профиля",
  key: "Поиск подключён",
  needs_key: "Требуется настройка",
  link: "Нет подключения",
};
const sent = new Set(["sent", "reported", "duplicate"]);
const hhApplicationReasons = {
  analysis_pending: "Ожидают анализа",
  analysis_error: "Ошибка анализа",
  overall_below_threshold: "Общая оценка ниже порога",
  skills_below_threshold: "Совпадение навыков ниже порога",
  missing_requirements: "Обязательные требования не подтверждены резюме",
  stale_analysis: "Нужен анализ по текущему резюме",
  letter_review: "Сопроводительное письмо требует проверки",
  search_filters: "Не проходят сохранённые фильтры",
  manual_review: "Нужно проверить форму HH вручную",
  unknown_outcome: "Требуют сверки отправки",
  manual_only: "Доступны для ручного отклика",
};
const hhApplicationResults = {
  sent: "Отправку подтвердил HH",
  unknown: "Результат отправки неизвестен",
  questions: "Нужно проверить форму HH вручную",
  duplicate: "Отклик уже есть на HH",
};

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
  const quickRequest = command === "search-start" || command.startsWith("search-status?") ||
    command === "resume-studio" || command === "ai-consent";
  const controller = quickRequest ? new AbortController() : undefined;
  const timer = controller && window.setTimeout(() => controller.abort(), 10000);
  let response;
  try {
    response = await fetch("/mini/api/" + command, {
      method: payload === undefined ? "GET" : "POST",
      headers: { Authorization: "tma " + auth, "Content-Type": "application/json" },
      body: payload === undefined ? undefined : JSON.stringify(payload),
      cache: "no-store",
      signal: controller?.signal,
    });
  } catch (error) {
    throw new Error(error.name === "AbortError" ? "Обновление заняло слишком много времени." :
      "Не удалось связаться с ботом. Проверьте интернет-соединение.");
  } finally {
    window.clearTimeout(timer);
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const fallback = {
      401: "Сессия Telegram истекла. Откройте приложение из чата с ботом заново.",
      429: "Слишком много запросов. Попробуйте немного позже.",
      502: "Bothost пока не может связаться с ботом.",
      503: "Бот временно недоступен.",
      504: "Bothost не дождался ответа бота.",
    };
    const error = new Error(data?.error || fallback[response.status] || "Не удалось выполнить запрос. Попробуйте ещё раз.");
    error.status = response.status;
    throw error;
  }
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
  if (tab === "search") renderSearch();
  else return refresh().catch(error => showError(error.message));
}

function sourceName(key) {
  return state.sources.find(source => source.id === key)?.name || "Ссылка";
}

function statusClass(status) {
  if (status === "sent" || status === "duplicate") return "status good";
  return "status" + (["unknown", "error", "questions"].includes(status) ? " attention" : "");
}

function canSend(job) {
  if (job.source === "hh") return state.hh_send && state.hh_connected && state.hh_resume && aiReady();
  if (job.source === "superjob") return state.superjob.send && state.superjob.connected && state.superjob.selected;
  return false;
}

async function openImport(job) {
  await changeTab("jobs");
  const form = byId("import-vacancy");
  form.open = true;
  if (job) {
    byId("job-url").value = job.url;
    byId("job-title").value = job.title;
    byId("job-company").value = job.company;
    byId("job-description").focus();
  } else {
    byId("job-url").focus();
  }
  form.scrollIntoView({ block: "start" });
}

async function showVacancy(job) {
  const detail = await api("vacancy?" + new URLSearchParams({ id: job.id }));
  byId("job-dialog-title").textContent = detail.title;
  const body = byId("job-description-body");
  body.replaceChildren(
    el("p", [detail.company, detail.region, detail.salary].filter(Boolean).join(" · "), "muted"),
  );
  const current = { ...job, ...detail, rank: detail.rank || job.rank };
  const duties = jobDuties(current, true);
  const assessment = discovery.assessment(current);
  if (assessment) body.append(assessment);
  if (duties) body.append(duties);
  body.append(el("p", detail.description || "В карточке нет подробного описания."));
  if (job.url) body.append(action("Карточка на сайте", () => external(job.url)));
  byId("job-dialog").showModal();
}

function jobDuties(job, full = false) {
  if (!Array.isArray(job.duties) || !job.duties.length) return null;
  const section = el("div", "", "job-duties");
  section.append(el("h4", "Что предстоит делать"));
  const list = el("ul");
  const shown = full ? job.duties.length : 2;
  list.append(...job.duties.slice(0, shown).map(duty => el("li", duty)));
  section.append(list);
  if (job.duties.length > shown) {
    const more = el("details", "", "extra-duties");
    const remaining = el("ul");
    remaining.append(...job.duties.slice(shown).map(duty => el("li", duty)));
    more.append(el("summary", "Ещё обязанности"), remaining);
    section.append(more);
  }
  return section;
}

function jobCard(job, history) {
  const card = el("article", "", "card");
  if (job.top) card.classList.add("recommended");
  card.dataset.jobId = job.id;
  const header = el("div", "", "card-header");
  header.append(el("span", sourceName(job.source), "chip"));
  if (job.label) header.append(el("span", job.label, statusClass(job.status)));
  if (job.top) {
    const badge = el("span", "", "top-label");
    const star = el("span", "★");
    star.setAttribute("aria-hidden", "true");
    badge.append(star, "Лучший вариант");
    header.prepend(badge);
  }
  card.append(header, el("h3", job.title), el("p", [job.company, job.region].filter(Boolean).join(" · "), "muted"),
    el("p", job.salary || "Зарплата не указана", "salary"));
  const assessment = discovery.assessment(job);
  if (assessment) card.append(assessment);
  const duties = jobDuties(job);
  if (duties) card.append(duties);
  if (!history) {
    const route = discovery.routeControls(job);
    if (route) card.append(route);
  }
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
    actions.append(discovery.compareButton(job));
    if (!provider?.in_app_apply) card.append(el("p", "Отклик можно отправить на сайте площадки.", "muted"));
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
  if (history && job.url && !canSend(job)) {
    actions.append(action("Открыть на площадке", () => external(job.url)));
    if (job.status === "deferred") {
      actions.append(action("Добавить описание", () => openImport(job)));
    }
    if (["queued", "draft", "approved", "deferred", "filtered", "questions", "error"].includes(job.status)) {
      actions.append(action("Отметить свой отклик", async () => {
        if (!await confirmAction("Вы уже отправили отклик на сайте площадки?")) return;
        await api("job", { id: job.id, action: "reported", confirm: true });
        await refresh();
        showFeedback("В журнал добавлена ваша отметка об отправке.");
      }));
    }
  }
  if (history && job.status === "unknown" && job.source === "hh") {
    const verify = action("Проверить отправку", async () => {
      const result = await api("job", { id: job.id, action: "reconcile" });
      await refresh();
      if (result.job?.status === "sent") showFeedback("Отправка подтверждена площадкой.");
    });
    verify.dataset.hhReconcile = "";
    actions.append(verify);
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
  discovery.render(searches);
}

function providerName(provider) {
  if (provider === "groq") return "Groq";
  if (provider === "gemini") return "Gemini";
  return "";
}

function aiReady() {
  return state.ai_available ?? Boolean(state.groq && state.consent);
}

function providerConfigured() {
  return Boolean(state.groq || state.gemini);
}

function dataAccessSection(id) {
  const target = byId(id);
  target.focus({ preventScroll: true });
  target.scrollIntoView({ block: "start" });
}

function queueExplanation(queue) {
  if (queue?.processing === false) return "Анализ приостановлен.";
  if (queue?.providers) {
    const allowed = Object.entries(queue.providers).filter(([name]) =>
      name === "groq" ? state.groq && state.consent : name === "gemini" && state.gemini && state.gemini_consent);
    const hasLimit = entry => entry.limit > 0 && entry.used >= entry.limit;
    const waiting = allowed.filter(([, entry]) => hasLimit(entry) || (entry.notice && entry.retry_at > Date.now() / 1000));
    if (!waiting.length || waiting.length < allowed.length) return "";
    return waiting.map(([name, entry]) => hasLimit(entry) ?
      "Дневной лимит " + providerName(name) + " исчерпан." : entry.notice).join(" ") + " Очередь сохранена.";
  }
  if (queue?.groq_limit > 0 && queue.groq_used >= queue.groq_limit) {
    return "Дневной лимит Groq исчерпан. Бот продолжит анализ после обновления лимита.";
  }
  return queue?.groq_notice || "";
}

function statusText(id, value) {
  const node = byId(id);
  node.hidden = !value;
  if (node.textContent !== value) node.textContent = value;
}

function activityList(id, entries) {
  const node = byId(id);
  node.hidden = !entries.length;
  node.replaceChildren(...entries.map(entry => el("li", entry)));
}

function activityDate(timestamp) {
  if (!(timestamp > 0)) return "";
  return new Date(timestamp * 1000).toLocaleString("ru-RU", { dateStyle: "medium", timeStyle: "short" });
}

function renderHHActivity() {
  const application = state.hh_application;
  byId("hh-activity").hidden = !application;
  if (!application) return;
  const blocked = application.blocked || {};
  const unknown = application.unknown ?? blocked.unknown_outcome ?? 0;
  const retry = activityDate(application.retry_at);
  statusText("hh-activity-message", [application.message, retry && "Следующая попытка возможна " + retry + "."]
    .filter(Boolean).join(" "));
  byId("hh-activity-eligible").textContent = application.eligible ?? 0;
  byId("hh-activity-pending").textContent = application.pending ?? blocked.analysis_pending ?? 0;
  byId("hh-activity-confirmed").textContent = application.confirmed_today ?? 0;
  statusText("hh-activity-verification", unknown ? "Требуют сверки на HH · " + unknown + "." : "");
  byId("hh-activity-check").hidden = !state.jobs.some(job => job.status === "unknown" && job.source === "hh");

  const reasons = Object.entries(hhApplicationReasons)
    .filter(([reason]) => reason !== "analysis_pending" && reason !== "unknown_outcome" && blocked[reason] > 0)
    .map(([reason, label]) => label + " · " + blocked[reason]);
  const examples = (application.examples || []).slice(0, 3).map(item =>
    item.title + " — " + (item.message || hhApplicationReasons[item.reason] || "Требуется проверка"));
  const recent = (application.recent || []).filter(item => hhApplicationResults[item.status]).map(item =>
    [item.title, hhApplicationResults[item.status], activityDate(item.at)].filter(Boolean).join(" · "));
  if (application.attempted_today > application.confirmed_today) {
    recent.unshift("Учтено попыток HH за сегодня · " + application.attempted_today + ".");
  }
  activityList("hh-activity-reasons", reasons);
  activityList("hh-activity-examples", examples);
  activityList("hh-activity-recent", recent);
  statusText("hh-activity-sample-note", application.sample_limited && reasons.length ?
    "Причины показаны по последним проверенным карточкам." : "");
  const hasReasons = reasons.length || examples.length;
  byId("hh-activity-details").hidden = !(hasReasons || recent.length);
  byId("hh-activity-summary").textContent = hasReasons ?
    recent.length ? "Причины и недавние попытки" : "Почему вакансии не готовы" : "Недавние попытки";
}

function renderJobs() {
  const filtered = state.jobs.filter(job =>
    (journalSource === "all" || job.source === journalSource) &&
    (selectedFilter === "all" || (selectedFilter === "sent") === sent.has(job.status)));
  const picker = byId("journal-source");
  const sources = [["all", "Все площадки"], ...state.sources.map(source => [source.id, source.name]), ["external", "Другие сайты"]];
  picker.replaceChildren(...sources.map(([id, name]) => {
    const option = el("option", name);
    option.value = id;
    return option;
  }));
  picker.value = journalSource;
  byId("total-count").textContent = state.summary.total;
  byId("sent-count").textContent = state.summary.sent;
  byId("today-count").textContent = state.summary.today + " / " + state.daily_limit;
  renderHHActivity();
  const analysis = byId("analysis-status");
  const queue = state.analysis_queue;
  analysis.hidden = !(queue?.pending > 0);
  analysis.textContent = queue?.pending > 0 ?
    ["В очереди на анализ · " + queue.pending + ".", queueExplanation(queue),
      providerName(queue?.provider) && "Последний ответ · " + providerName(queue.provider) + "."].filter(Boolean).join(" ") : "";
  const setup = byId("jobs-setup");
  setup.hidden = !(queue?.pending || state.jobs.some(job => job.status === "queued")) || Boolean(state.resume && aiReady());
  setup.replaceChildren(
    el("span", "Для писем нужен профиль и согласие на анализ. "),
    action("Открыть профиль", async () => {
      await changeTab("connections");
      dataAccessSection(state.resume && state.gemini && !state.groq ? "gemini-title" : "profile-title");
    })
  );
  if (!filtered.length) {
    byId("jobs").replaceChildren(el("p", journalSource !== "all" ? "Здесь пока нет карточек этой площадки." :
      selectedFilter === "sent" ? "Отправленных откликов пока нет." : "Подготовьте отклик из результатов поиска.", "empty"));
    return;
  }
  const ordered = selectedFilter === "sent"
    ? filtered : [...filtered].sort((a, b) => Number(Boolean(b.top)) - Number(Boolean(a.top)));
  byId("jobs").replaceChildren(...ordered.map(job => jobCard(job, true)));
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

function hhDialogButton(id, label, dialog, open, primary = false) {
  const button = action(label, open, primary);
  button.id = id;
  button.setAttribute("aria-haspopup", "dialog");
  button.setAttribute("aria-controls", dialog.id);
  return button;
}

function showHHWindow() {
  const selecting = hhWindow.mode === "resume";
  const editing = hhWindow.mode === "edit";
  hhDialog.dataset.mode = hhWindow.mode;
  byId("hh-dialog-title").textContent = editing ? "Редактирование резюме HH" :
    selecting ? "Выбор резюме в HH" : "Подключение HH";
  hhFrame.title = byId("hh-dialog-title").textContent;
  byId("hh-edit-tools").hidden = !editing;
  if (editing) {
    statusText("hh-edit-status", "");
    setHHEditHelp();
  }
  hhFrame.src = hhWindow.url;
  hhDialog.showModal();
}

async function openHHWindow(mode, fresh = false) {
  const { url } = mode === "resume" ? await api("hh-browser", {}) : await api("connect", { fresh });
  hhWindow = { url, mode };
  showHHWindow();
  await refresh();
}

function showHHResumeUrlError(message = "") {
  const error = byId("hh-resume-url-error");
  error.textContent = message;
  error.hidden = !message;
  if (message) {
    hhResumeUrlField.setAttribute("aria-invalid", "true");
    hhResumeUrlField.setAttribute("aria-describedby", "hh-resume-url-help hh-resume-url-error");
  } else {
    hhResumeUrlField.removeAttribute("aria-invalid");
    hhResumeUrlField.setAttribute("aria-describedby", "hh-resume-url-help");
  }
}

function openHHResumeUrl() {
  if (byId("save-hh-resume-url").disabled) return;
  hhResumeUrlField.value = state.hh_resume_url || "";
  showHHResumeUrlError();
  hhResumeUrlDialog.showModal();
  if (hhResumeUrlField.value) hhResumeUrlField.select();
}

function showHHResumeBound(next) {
  profileDirty = false;
  applyState(next);
  showFeedback(hhResumeBoundMessage);
}

async function loadHHResumes() {
  if (loadingHHResumes) return;
  const previous = state;
  loadingHHResumes = true;
  showFeedback("Загружаю резюме из HH…");
  let next;
  try {
    next = await api("hh-resumes", {});
  } catch (error) {
    showFeedback();
    throw error;
  } finally {
    loadingHHResumes = false;
  }
  if (next.hh_resume_id && (next.hh_resume_id !== previous.hh_resume_id || next.resume !== previous.resume)) {
    showHHResumeBound(next);
    return;
  }
  applyState(next);
  showFeedback(Object.keys(next.resumes).length ? "Список резюме HH обновлён." :
    "Откройте своё резюме в HH или вставьте ссылку на него.");
}

function renderHHAuto() {
  const card = byId("hh-auto-card");
  card.hidden = !state.hh_connected || state.hh_login_open || !state.auto_settings;
  if (card.hidden) return;
  const persistent = state.storage_persistent ?? state.persistent;
  const status = byId("hh-auto-state");
  const operations = byId("hh-auto-controls");
  status.textContent = state.hh_access_restricted ? "Доступ ограничен" : state.hh_auto ? "Включены" : "Выключены";
  status.className = state.hh_auto ? "status good" : "status";
  if (!skillsDirty) byId("hh-skills-score").value = state.auto_settings.skills_score;
  byId("hh-skills-help").textContent = "Анализ сверяет навыки с резюме. Для автоотклика также нужны " +
    "общая оценка от " + state.auto_settings.score + "% и подтверждённые обязательные требования.";
  byId("hh-storage-help").hidden = persistent;
  const blockers = [
    !persistent && "Сохранение резюме, входа в HH и журнала после обновления пока не подтверждено. Автоотклики доступны с постоянным диском.",
    !(state.hh_resume && state.resume) && "Выберите резюме в аккаунте HH.",
    providerConfigured() && !aiReady() && "Разрешите Groq или Gemini обрабатывать профессиональные сведения.",
    !providerConfigured() && "Добавьте ключ Groq или Gemini в настройки бота на Bothost.",
    persistent && !state.hh_auto_allowed && "Разрешите автоотклики в настройках бота на Bothost (AUTO_SEND).",
  ].filter(Boolean);
  statusText("hh-auto-help", state.hh_application?.message || "");
  activityList("hh-auto-blockers", blockers);
  operations.replaceChildren();
  if (state.hh_access_restricted) {
    operations.append(action("Проверить доступ HH", async () => {
      const next = await api("hh-check", {});
      applyState(next);
      showFeedback(next.hh_access_restored ?
        "Страница открывается. Теперь можно включить автоотклики." :
        "HH по-прежнему ограничивает доступ из браузера бота.");
    }, true));
    const source = state.sources.find(item => item.id === "hh");
    if (source) operations.append(action("Открыть вакансии на HH", () => external(source.url)));
    operations.append(action("Добавить вакансию по ссылке", () => openImport()));
  } else if (state.hh_auto) {
    operations.append(action("Оставить только поиск", async () => {
      applyState(await api("hh-mode", { mode: "search" }));
      showFeedback("Автоотклики выключены. Поиск продолжается.");
    }));
  } else if (!blockers.length) {
    operations.append(action("Включить автоотклики", async () => {
      const message = "Отправлять резюме и письма работодателям HH при совпадении навыков от " +
        state.auto_settings.skills_score + "%? Суточный лимит · " + state.daily_limit;
      if (await confirmAction(message)) {
        applyState(await api("hh-mode", { mode: "auto", confirm: true }));
        showFeedback("Автоотклики HH включены.");
      }
    }, true));
  } else if (providerConfigured() && !aiReady()) {
    operations.append(action("Настроить анализ", () =>
      dataAccessSection(state.groq ? "profile-title" : "gemini-title")));
  }
  if (state.hh_application) operations.append(action("Проверить отклики", () => changeTab("jobs")));
  operations.hidden = !operations.childElementCount;
}

function renderHH(card, operations) {
  const connected = state.hh_connected;
  const hasResume = connected && state.hh_resume;
  if (hasResume) card.append(el("p", "Резюме для откликов: " + state.hh_resume, "resume-name"));
  if (connected && !hasResume) {
    card.append(el("p", state.hh_resumes_status || "Можно искать вакансии. Для откликов выберите резюме HH.", "muted"));
  } else if (connected && state.hh_resumes_status) {
    card.append(el("p", state.hh_resumes_status, "muted"));
  }
  if (!connected && state.hh_progress) card.append(el("p", state.hh_progress, "muted"));
  if (state.hh_running) {
    card.append(el("p", state.hh_auto ? "Автоотклики включены" : "Фоновый поиск включён", "muted"));
    card.append(el("p", "Фоновый поиск использует сохранённые фильтры. Для разового поиска можно изменить параметры на вкладке «Поиск».", "muted"));
    if (!state.hh_login_open) operations.append(action("Найденные вакансии", () => changeTab("jobs")));
  }
  if (state.hh_notice && connected && !state.hh_login_open) {
    card.append(el("p", state.hh_notice, "muted"));
  }
  if (state.hh_login_open) {
    if (!hhDialog.open) {
      if (hhWindow) operations.append(hhDialogButton("hh-continue",
        hhWindow.mode === "resume" ? "Продолжить выбор резюме" : "Продолжить вход", hhDialog, showHHWindow, true));
      const cancel = action(hhWindow?.mode === "login" ? "Отменить вход" : "Закрыть окно HH", async () => {
        await api("cancel", {});
        hhWindow = undefined;
        await refresh();
      });
      cancel.id = "hh-cancel";
      operations.append(cancel);
    }
    return;
  }
  if (!connected) operations.append(hhDialogButton("hh-connect", "Подключить HH", hhDialog,
    () => openHHWindow("login"), true));
  if (connected && !state.hh_running) {
    operations.append(action("Включить фоновый поиск", async () => {
      await api("hh-mode", { mode: "search" });
      await refresh();
      showFeedback("Фоновый поиск HH включён.");
    }, true));
  }
  if (connected) {
    operations.append(
      hhDialogButton("hh-find-resume", "Найти резюме в HH", hhDialog, () => openHHWindow("resume")),
      hhDialogButton("hh-resume-url-open", "Вставить ссылку на своё резюме", hhResumeUrlDialog, openHHResumeUrl)
    );
    const resumeButton = action("Загрузить список резюме", loadHHResumes);
    resumeButton.disabled = loadingHHResumes;
    operations.append(resumeButton);
  }
  if (connected || state.hh_has_session) {
    operations.append(hhDialogButton("hh-switch", "Сменить аккаунт", hhDialog,
      () => openHHWindow("login", true)));
    operations.append(action("Отключить", async () => {
      if (await confirmAction("Закрыть сохранённую сессию HH?")) {
        await api("disconnect", { confirm: true });
        hhWindow = undefined;
        await refresh();
      }
    }));
  }
  if (connected && Object.keys(state.resumes).length && (!hasResume || Object.keys(state.resumes).length > 1)) {
    card.append(resumePicker("Выберите резюме HH", state.resumes, state.hh_resume_id, async id => {
      showHHResumeBound(await api("resume", { id }));
    }));
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

function renderHabr(card, operations) {
  const account = state.habr;
  if (account?.profile?.login) card.append(el("p", "@" + account.profile.login, "resume-name"));
  card.append(el("p", account?.status ||
    "Профиль можно подтвердить через Хабр Аккаунт. Отклики отправляются на Хабр Карьере.", "muted"));
  if (!account?.enabled) {
    if (account?.applications_url) operations.append(action("Настроить OAuth", () => external(account.applications_url)));
    return;
  }
  if (pendingHabr && !account.status.startsWith("Завершите вход")) pendingHabr = "";
  if (pendingHabr) {
    operations.append(action("Войти через Хабр Аккаунт", () => external(pendingHabr), true));
  }
  operations.append(action(account.profile.login ? "Подтвердить профиль заново" : "Подтвердить профиль", async () => {
    pendingHabr = (await api("habr", { action: "connect" })).url;
    await refresh();
  }, !pendingHabr));
  if (account.profile.login) operations.append(action("Убрать профиль", async () => {
    if (!await confirmAction("Убрать связь с профилем Хабр Карьеры из Mini App?")) return;
    pendingHabr = "";
    applyState(await api("habr", { action: "disconnect", confirm: true }));
  }));
}

function renderHubSearch(card, operations, source) {
  const search = state.hub?.[source.id];
  if (!search?.available) return;
  if (search.enabled) {
    card.append(el("p", ["Фоновый поиск включён.", search.message,
      search.last_at && "Последняя проверка " + activityDate(search.last_at) + "."].filter(Boolean).join(" "), "muted"));
  }
  operations.append(action(search.enabled ? "Остановить автопоиск" : "Включить автопоиск", async () => {
    applyState(await api("hub", { source: source.id, enabled: !search.enabled }));
  }));
}

function renderStudioNotes(id, notes) {
  const section = byId(id);
  const values = Array.isArray(notes) ? notes.filter(value => typeof value === "string" && value.trim()) : [];
  section.hidden = !values.length;
  section.querySelector("ul").replaceChildren(...values.map(value => el("li", value)));
}

function resumeStudioBusy() {
  return resumeStudio.starting || state.resume_studio_status?.status === "processing";
}

function resumeStudioLinkedHH() {
  return Boolean(state.hh_connected || state.hh_resume_id);
}

function renderResumeStudio() {
  const draft = state.resume_studio;
  const result = state.resume_studio_status;
  const busy = resumeStudioBusy();
  if (!resumeStudio.initialized) {
    byId("resume-studio-mode").value = draft?.mode || (state.resume ? "improve" : "build");
    byId("resume-studio-target").value = draft?.target || byId("query").value || state.defaults.query;
    resumeStudio.initialized = true;
  }
  const consent = byId("resume-studio-consent");
  byId("resume-studio-consent-label").hidden = Boolean(state.consent) || !state.groq;
  if (state.consent) consent.checked = false;
  byId("resume-studio-gemini-jump").hidden = !state.gemini || state.gemini_consent;
  const aiHelp = byId("resume-studio-ai-help");
  if (state.groq && state.gemini && state.gemini_consent) {
    aiHelp.textContent = "Для черновика обрабатываются профессиональные сведения профиля и факты из формы. При недоступности Groq используется Gemini.";
  } else if (state.groq) {
    aiHelp.textContent = "Для черновика Groq обработает профессиональные сведения профиля и факты из формы.";
  } else if (state.gemini) {
    aiHelp.textContent = "Для черновика Gemini обработает профессиональные сведения профиля и факты из формы после вашего разрешения.";
  } else {
    aiHelp.textContent = "Для подготовки текста настройте Groq или Gemini на Bothost. Уточняющие вопросы доступны без подключения.";
  }
  const generate = byId("resume-studio-generate");
  generate.disabled = busy || resumeStudio.adopting;
  generate.textContent = busy ? "Готовлю черновик…" : "Подготовить черновик";
  byId("resume-studio-form").setAttribute("aria-busy", String(busy));

  const status = byId("resume-studio-status");
  const problem = resumeStudio.formError || (busy ? resumeStudio.pollError : result?.message);
  const message = problem || (busy ?
    "Подготовка может занять до минуты. Можно продолжить пользоваться приложением." :
    result?.status === "ready" ? (draft?.text ? "Черновик готов." : "Добавьте сведения, чтобы подготовить текст.") : "");
  status.classList.toggle("form-error", Boolean(problem));
  status.hidden = !message;
  if (status.textContent !== message) status.textContent = message;
  byId("resume-studio-check").hidden = !resumeStudio.pollError || !busy;

  byId("resume-studio-result").hidden = !draft;
  if (draft) {
    const hasText = Boolean(draft.text);
    const linked = resumeStudioLinkedHH();
    const inUse = hasText && draft.text === state.resume;
    byId("resume-studio-result-title").textContent = busy ? "Предыдущий черновик" : "Черновик резюме";
    byId("resume-studio-result-target").textContent = draft.target;
    byId("resume-studio-review").hidden = !hasText;
    byId("resume-studio-document").hidden = !hasText;
    const preview = byId("resume-studio-preview");
    if (preview.value !== draft.text) preview.value = draft.text || "";
    byId("resume-studio-adopt").hidden = linked || inUse;
    byId("resume-studio-adopt").disabled = busy || resumeStudio.adopting;
    byId("resume-studio-hh-help").hidden = !linked;
    byId("resume-studio-in-use").hidden = linked || !inUse;
    statusText("resume-studio-sample", [
      providerName(draft.provider),
      draft.sample_count > 0 && "Вакансий учтено: " + draft.sample_count,
    ].filter(Boolean).join(" · "));
    renderStudioNotes("resume-studio-suggestions", draft.suggestions);
    renderStudioNotes("resume-studio-questions", draft.questions);
  }
  if (result?.status !== "processing") {
    window.clearTimeout(resumeStudio.timer);
    resumeStudio.timer = undefined;
    resumeStudio.pollError = "";
  } else if (!resumeStudio.timer && !resumeStudio.checking && !resumeStudio.pollError) {
    resumeStudio.timer = window.setTimeout(checkResumeStudio, 1500);
  }
}

function updateResumeStudio(next) {
  state = {
    ...state,
    resume_studio: next.resume_studio,
    resume_studio_status: next.resume_studio_status,
    consent: next.consent ?? state.consent,
    gemini_consent: next.gemini_consent ?? state.gemini_consent,
    ai_available: next.ai_available ?? state.ai_available,
  };
}

async function checkResumeStudio() {
  window.clearTimeout(resumeStudio.timer);
  resumeStudio.timer = undefined;
  if (resumeStudio.checking) return;
  resumeStudio.checking = true;
  resumeStudio.pollError = "";
  try {
    updateResumeStudio(await api("resume-studio"));
  } catch (error) {
    resumeStudio.pollError = error.message;
  } finally {
    resumeStudio.checking = false;
    renderResumeStudio();
  }
}

function renderConnections() {
  renderHHAuto();
  const cards = [];
  for (const source of state.sources) {
    const card = el("article", "", "card");
    const head = el("div", "", "card-header");
    const habrLinked = source.id === "habr" && state.habr?.profile?.login;
    const linked = (source.id === "hh" && state.hh_connected && !state.hh_access_restricted) ||
      (source.id === "superjob" && state.superjob.connected && !state.superjob.status.startsWith("Нужно снова"));
    const badge = habrLinked ? "Профиль подтверждён" : source.id === "hh" && state.hh_access_restricted ?
      "Доступ ограничен" : linked ? "Подключён" : source.mode === "link" ? "Вход на сайте" : modes[source.mode];
    head.append(el("h3", source.name), el("span", badge, linked || habrLinked ? "status good" : "status"));
    card.append(head);
    const operations = el("div", "", "actions");
    if (source.id === "hh" && source.mode === "browser") renderHH(card, operations);
    else if (source.id === "superjob") renderSuperjob(card, operations);
    else if (source.id === "habr") renderHabr(card, operations);
    else card.append(el("p", source.id === "trudvsem" ?
      "Вакансии доступны в Mini App. В личный кабинет можно войти на портале через Госуслуги." :
      "Войдите на сайте площадки. Вакансии можно добавить в общий журнал по ссылке.", "muted"));
    renderHubSearch(card, operations, source);
    if (source.account_url) operations.append(action("Открыть аккаунт на сайте", () => external(source.account_url)));
    if (source.url && (source.id !== "hh" || !state.hh_access_restricted)) {
      operations.append(action("Вакансии на сайте", () => external(source.url)));
    }
    if (operations.childElementCount) card.append(operations);
    cards.push(card);
  }
  byId("connections").replaceChildren(...cards);
  byId("profile-info").textContent = [
    state.hh_connected && state.hh_resume ? "Резюме HH уже в профиле." :
      state.resume ? "Резюме сохранено." :
      state.hh_connected ? "Выберите резюме HH для подготовки писем." : "Подключите HH, чтобы загрузить резюме из аккаунта.",
    !providerConfigured() ? "Для писем нужен подключённый Groq или Gemini." : "",
  ].filter(Boolean).join(" ");
  byId("groq-consent-label").hidden = !state.groq && Boolean(state.gemini);
  if (!profileDirty) {
    byId("resume").value = state.resume;
    byId("consent").checked = state.consent;
  }
  byId("gemini-access").hidden = !state.gemini;
  byId("gemini-title").textContent = state.groq ? "Резервный анализ Gemini" : "Анализ Gemini";
  const gemini = byId("gemini-status");
  gemini.textContent = state.gemini_consent ? "Разрешён" : "Отключён";
  gemini.className = state.gemini_consent ? "status good" : "status";
  byId("gemini-info").textContent = state.groq ?
    "Если Groq достигнет лимита или временно недоступен, Gemini сможет обработать профессиональные сведения для анализа, писем и резюме." :
    "После вашего разрешения Gemini сможет обрабатывать профессиональные сведения для анализа, писем и резюме.";
  byId("gemini-consent").textContent = state.gemini_consent ? "Отозвать разрешение Gemini" : "Разрешить Gemini";
  renderResumeStudio();
  renderHHEditor();
}

function hhEditLabel(kind, item) {
  return kind === "skills" ? item.name : [item.role, item.company].filter(Boolean).join(" · ");
}

function setHHEditHelp() {
  const skill = byId("hh-edit-change").value.startsWith("skills:");
  byId("hh-edit-help").textContent = skill ?
    "В блоке «Навыки» нажмите пустое поле, заполните его и выберите подсказку HH. Сохраните изменения на сайте." :
    "Откройте указанное место работы и нажмите поле «Обязанности и достижения». После заполнения сохраните изменения на HH.";
}

function renderHHEditor() {
  const section = byId("hh-resume-editor");
  section.hidden = !state.hh_resume_id;
  if (section.hidden) return;
  const plan = state.hh_editor;
  const busy = state.hh_editor_pending;
  byId("hh-editor-resume").textContent = state.hh_resume;
  const prepare = byId("hh-editor-prepare");
  prepare.disabled = Boolean(busy || !state.hh_connected || state.hh_access_restricted || !aiReady());
  statusText("hh-editor-status", busy ? "Читаю опыт на HH и подбираю правки…" :
    state.hh_editor_error ||
    (state.hh_access_restricted ? "Проверьте доступ или откройте карточку на самом HH." :
      !aiReady() ? "Разрешите анализ в Groq или Gemini." :
      plan ? "Проверьте предложения и выберите правку для вашего резюме HH." : ""));
  const changes = byId("hh-editor-changes");
  changes.replaceChildren();
  if (plan) {
    for (const [kind, title] of [["skills", "Ключевые навыки"], ["duties", "Обязанности по местам работы"]]) {
      if (!plan[kind]?.length) continue;
      changes.append(el("h4", title));
      plan[kind].forEach((item, index) => {
        const verified = plan.verified?.[kind]?.includes(index);
        const block = el("details", "", "hh-edit-proposal card");
        block.append(el("summary", hhEditLabel(kind, item) + (verified ? " · Сохранено на HH" : "")));
        const value = kind === "skills" ? item.name : item.text;
        block.append(el("p", value));
        if (kind === "skills" && plan.jobs?.[item.job]) {
          block.append(el("p", hhEditLabel("duties", plan.jobs[item.job]), "muted"));
        }
        const proof = kind === "skills" ? [item.quote] : item.proof.map(point => point.quote);
        block.append(el("p", "Из исходных обязанностей\n" + proof.join("\n"), "muted"));
        const buttons = el("div", "", "actions");
        if (!verified && !busy && state.hh_connected && !state.hh_access_restricted) {
          buttons.append(action("Открыть поле на HH", () => openHHEditor(kind, index), true));
        }
        if (navigator.clipboard) buttons.append(action("Скопировать", async () => {
          await navigator.clipboard.writeText(value);
          showFeedback("Правка скопирована.");
        }));
        block.append(buttons);
        changes.append(block);
      });
    }
    if (!changes.childElementCount) changes.append(el("p", "По этому опыту модель не нашла подтверждённых дополнений.", "muted"));
  }
  if (busy && !hhEditorTimer) hhEditorTimer = window.setTimeout(pollHHEditor, 1700);
}

async function pollHHEditor() {
  window.clearTimeout(hhEditorTimer);
  hhEditorTimer = undefined;
  try {
    const next = await api("hh-editor");
    if (!next.hh_editor_pending) await refresh();
    else {
      Object.assign(state, next);
      renderHHEditor();
    }
  } catch (error) {
    statusText("hh-editor-status", error.message);
    byId("hh-editor-prepare").disabled = false;
  }
}

async function openHHEditor(kind, index) {
  const plan = state.hh_editor;
  const { url } = await api("hh-editor", { action: "open", revision: plan.revision });
  const select = byId("hh-edit-change");
  select.replaceChildren();
  for (const field of ["skills", "duties"]) {
    plan[field].forEach((item, position) => {
      const option = el("option", hhEditLabel(field, item));
      option.value = field + ":" + position;
      select.append(option);
    });
  }
  select.value = kind + ":" + index;
  hhWindow = { url, mode: "edit", revision: plan.revision };
  showHHWindow();
}

function applyState(next, first = false) {
  state = next;
  discovery.prepPreferences(state.preferences, first);
  if (!state.hh_login_open && !hhDialog.open) hhWindow = undefined;
  const version = byId("app-version");
  version.hidden = !state.version;
  version.textContent = state.version ? "Версия " + state.version : "";
  if (first) {
    for (const key of ["query", "region", "salary"]) byId(key).value = state.defaults[key];
    byId("search-related").checked = Boolean(state.defaults.use_related);
    for (const source of state.sources) {
      if (source.in_app_search) byId("source").append(new Option(source.name, source.id));
    }
    byId("launch").hidden = true;
    byId("navigation").hidden = byId("panel-search").hidden = false;
  }
  if (selectedTab === "jobs") renderJobs();
  if (selectedTab === "connections") renderConnections();
}

async function refresh(first = false, unchanged = false) {
  const next = await api("state");
  if (unchanged && JSON.stringify(state) === JSON.stringify(next)) return;
  applyState(next, first);
}

const discovery = vacancyDiscovery({
  byId,
  el,
  api,
  action,
  providerName,
  makeCard: jobCard,
  getPreferences: () => state.preferences || {},
  updateState: preferences => applyState({ ...state, preferences }),
  showError,
  showFeedback,
});

function searchUpdate(message = "", retryLabel = "") {
  const status = byId("search-status");
  const retry = byId("search-retry");
  if (status.textContent !== message) status.textContent = message;
  byId("search-update").hidden = !message;
  retry.hidden = !retryLabel;
  retry.textContent = retryLabel;
}

function searchButton(busy) {
  const submit = byId("search-submit");
  submit.disabled = busy;
  submit.textContent = busy ? "Ищу…" : "Найти";
}

function showSearchResults(search, result) {
  const jobs = result.jobs;
  if (jobs.length || result.done) {
    // Compare with the last response so local map and comparison edits are kept until new results arrive.
    const snapshot = JSON.stringify(jobs);
    if (snapshot !== search.jobsSnapshot) {
      const previous = new Map(searches.map(job => [job.id, job]));
      searches = jobs.map(job => {
        const earlier = previous.get(job.id);
        return JSON.stringify(earlier) === JSON.stringify(job) ? earlier : job;
      });
      search.jobsSnapshot = snapshot;
      renderSearch();
    }
    const duplicates = result.stats?.duplicates || 0;
    byId("result-count").textContent = "Найдено: " + searches.length +
      (duplicates ? " · Скрыто дублей: " + duplicates : "");
  }
  const notes = JSON.stringify(result.notices);
  if (search.noticeSnapshot !== notes) {
    notices.replaceChildren(...result.notices.map(notice => {
      const node = el("div", "", "source-note");
      node.append(el("p", sourceName(notice.source) + " · " + notice.message, "muted"));
      const source = state.sources.find(item => item.id === notice.source);
      if (notice.source === "hh" && state.hh_access_restricted && source) {
        node.append(action("Поиск на HH", () => external(source.url)));
        node.append(action("Добавить ссылку", () => openImport()));
      } else {
        node.append(action("Аккаунты", () => changeTab("connections")));
      }
      return node;
    }));
    search.noticeSnapshot = notes;
  }
  if (!result.done) {
    const sources = Array.isArray(result.pending_sources) ? result.pending_sources.map(sourceName).join(", ") : "";
    const progress = sources ? "Поиск продолжается · " + sources + "." : "Ищу по выбранным площадкам…";
    searchUpdate(progress + (!jobs.length && searches.length ? " Пока показаны предыдущие результаты." : ""));
  } else if (result.analysis_pending > 0) {
    const explanation = queueExplanation(result.analysis_queue) || (result.analysis_running
      ? "Оценки появятся по мере обработки." : "Проверьте настройки анализа в аккаунтах.");
    searchUpdate("В очереди на анализ · " + result.analysis_pending + ". " + explanation);
  } else if (jobs.length) {
    searchUpdate();
  } else {
    searchUpdate(result.notices.length ? "Вакансий пока нет. Проверьте сообщения площадок ниже." :
      "Вакансии не найдены. Попробуйте изменить параметры поиска.");
  }
}

function scheduleSearchPoll(search, delay) {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => pollSearch(search), delay);
}

async function pollSearch(search) {
  if (activeSearch !== search || search.polling || startingSearch) return;
  window.clearTimeout(searchTimer);
  if (document.visibilityState !== "visible") return;
  search.polling = true;
  try {
    const result = await api("search-status?" + new URLSearchParams({ search_id: search.id }));
    if (activeSearch !== search || startingSearch) return;
    search.failures = 0;
    searchButton(!result.done);
    showSearchResults(search, result);
    if (!result.done) scheduleSearchPoll(search, 2000);
    else if (result.analysis_pending > 0) {
      const working = result.analysis_running && !queueExplanation(result.analysis_queue);
      scheduleSearchPoll(search, working ? 12000 : 30000);
    }
  } catch (error) {
    if (activeSearch !== search) return;
    if (error.status === 410 || error.status === 404) {
      activeSearch = undefined;
      searchButton(false);
      searchUpdate(error.message, "Повторить поиск");
      return;
    }
    if ([401, 403].includes(error.status)) {
      activeSearch = undefined;
      searchButton(false);
      searchUpdate(error.message);
      return;
    }
    search.failures += 1;
    searchUpdate(search.failures < 3 ? error.message + " Пытаюсь обновить результаты." :
      error.message + " Результаты можно обновить вручную.", "Обновить результаты");
    if (search.failures < 3) scheduleSearchPoll(search, 3000 * search.failures);
    else searchButton(false);
  } finally {
    search.polling = false;
  }
}

async function beginSearch(query) {
  if (startingSearch) return;
  startingSearch = true;
  showError();
  showFeedback();
  searchButton(true);
  searchUpdate(searches.length ? "Ищу вакансии. Пока показаны предыдущие результаты." : "Ищу по выбранным площадкам…");
  lastSearchQuery = query;
  try {
    const result = await api("search-start", query);
    if (!result.search_id) throw new Error("Бот не подтвердил начало поиска.");
    activeSearch = { id: result.search_id, failures: 0 };
    notices.replaceChildren();
  } catch (error) {
    searchButton(false);
    const retryLabel = [401, 403].includes(error.status) ? "" : "Повторить поиск";
    searchUpdate(error.message, retryLabel);
    return;
  } finally {
    startingSearch = false;
  }
  await pollSearch(activeSearch);
}

byId("search-form").addEventListener("submit", event => {
  event.preventDefault();
  const query = Object.fromEntries(new FormData(event.target));
  for (const flag of ["hh_title_only", "include_unknown_commute", "include_related"]) {
    if (!(flag in query)) query[flag] = "false";
  }
  query.sort = discovery.resultSort();
  beginSearch(query);
});
byId("search-retry").addEventListener("click", () => {
  if (byId("search-retry").textContent === "Повторить поиск" && lastSearchQuery) beginSearch(lastSearchQuery);
  else if (activeSearch) {
    activeSearch.failures = 0;
    pollSearch(activeSearch);
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
    journalSource = result.job.source;
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

byId("gemini-consent").addEventListener("click", async event => {
  const button = event.currentTarget;
  const consent = !state.gemini_consent;
  showError();
  showFeedback();
  button.disabled = true;
  try {
    applyState(await api("ai-consent", { gemini: consent }));
    showFeedback(consent ? "Обработка в Gemini разрешена." : "Разрешение для Gemini отозвано.");
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
  }
});

byId("resume-studio-jump").addEventListener("click", () => {
  dataAccessSection(state.hh_resume_id ? "hh-editor-title" : "resume-studio-title");
});
byId("hh-editor-native").addEventListener("click", () => external(state.hh_resume_url));
byId("hh-editor-prepare").addEventListener("click", async () => {
  const button = byId("hh-editor-prepare");
  button.disabled = true;
  try {
    Object.assign(state, await api("hh-editor", { action: "prepare" }));
    renderHHEditor();
  } catch (error) {
    statusText("hh-editor-status", error.message);
    button.disabled = false;
  }
});
byId("hh-edit-change").addEventListener("change", setHHEditHelp);
byId("hh-edit-fill").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const [kind, index] = byId("hh-edit-change").value.split(":");
    await api("hh-editor", { action: "fill", revision: hhWindow.revision, kind, index: Number(index) });
    statusText("hh-edit-status", kind === "skills" ?
      "Название внесено в поле. Выберите навык из подсказки HH и сохраните." :
      "Поле заполнено. Проверьте его и нажмите «Сохранить» на HH.");
  } catch (error) {
    statusText("hh-edit-status", error.message);
  } finally {
    button.disabled = false;
  }
});
byId("hh-edit-verify").addEventListener("click", async event => {
  const button = event.currentTarget;
  if (!await confirmAction("Вы уже нажали «Сохранить» на HH? Проверка повторно откроет карточку.")) return;
  button.disabled = true;
  try {
    const next = await api("hh-editor", { action: "verify", revision: hhWindow.revision, saved: true });
    Object.assign(state, next);
    const confirmed = next.hh_editor?.verified;
    const count = (confirmed?.skills.length || 0) + (confirmed?.duties.length || 0);
    statusText("hh-edit-status", count ? "Найдено сохранённых правок: " + count + ". Резюме для анализа обновлено." :
      "В карточке HH пока нет этих правок. Проверьте сохранение на сайте.");
    renderHHEditor();
  } catch (error) {
    statusText("hh-edit-status", error.message);
  } finally {
    button.disabled = false;
  }
});
byId("resume-studio-gemini-jump").addEventListener("click", () => dataAccessSection("gemini-title"));
byId("resume-studio-check").addEventListener("click", checkResumeStudio);
byId("resume-studio-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (resumeStudioBusy() || resumeStudio.adopting) return;
  showError();
  showFeedback();
  const consent = !state.consent && byId("resume-studio-consent").checked;
  resumeStudio.starting = true;
  resumeStudio.formError = resumeStudio.pollError = "";
  renderResumeStudio();
  try {
    const next = await api("resume-studio", {
      action: "generate",
      mode: byId("resume-studio-mode").value,
      target: byId("resume-studio-target").value.trim(),
      facts: byId("resume-studio-facts").value.trim(),
      ...(consent ? { consent: true } : {}),
    });
    updateResumeStudio(next);
    if (consent) refresh(false, true).catch(() => {});
  } catch (error) {
    resumeStudio.formError = error.message;
    refresh(false, true).catch(() => {});
  } finally {
    resumeStudio.starting = false;
    renderResumeStudio();
  }
});

byId("resume-studio-export").addEventListener("click", async () => {
  const text = state.resume_studio?.text;
  if (!text) return;
  const file = new File([text, "\n"], "resume.txt", { type: "text/plain;charset=utf-8" });
  try {
    if (navigator.canShare?.({ files: [file] })) {
      await navigator.share({ files: [file] });
      return;
    }
  } catch (error) {
    if (error.name === "AbortError") return;
  }
  const url = URL.createObjectURL(file);
  const download = document.createElement("a");
  download.href = url;
  download.download = file.name;
  document.body.append(download);
  download.click();
  download.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60000);
});

byId("resume-studio-copy").addEventListener("click", async () => {
  const preview = byId("resume-studio-preview");
  if (!preview.value) return;
  try {
    await navigator.clipboard.writeText(preview.value);
    showFeedback("Текст резюме скопирован.");
  } catch {
    preview.focus();
    preview.select();
    showFeedback("Текст выделен. Выберите «Скопировать» в меню телефона.");
  }
});

byId("resume-studio-adopt").addEventListener("click", async () => {
  const text = state.resume_studio?.text;
  if (!text || text === state.resume || resumeStudioBusy() || resumeStudio.adopting || resumeStudioLinkedHH()) return;
  resumeStudio.adopting = true;
  resumeStudio.formError = "";
  renderResumeStudio();
  try {
    if (!await confirmAction("Черновик заменит текст профиля для анализа вакансий и подготовки писем. Использовать его?")) return;
    const next = await api("resume-studio", { action: "adopt", confirm: true });
    profileDirty = false;
    applyState(next);
    showFeedback("Резюме сохранено в профиле для анализа вакансий.");
  } catch (error) {
    resumeStudio.formError = error.message;
  } finally {
    resumeStudio.adopting = false;
    renderResumeStudio();
  }
});

byId("hh-auto-form").addEventListener("input", () => { skillsDirty = true; });
byId("hh-auto-form").addEventListener("submit", async event => {
  event.preventDefault();
  const save = byId("save-hh-skills");
  const field = byId("hh-skills-score");
  if (save.disabled) return;
  showError();
  showFeedback();
  save.disabled = field.readOnly = true;
  try {
    const threshold = field.valueAsNumber;
    if (!Number.isInteger(threshold) || threshold < 1 || threshold > 100) {
      throw new Error("Укажите целый процент от 1 до 100.");
    }
    const next = await api("auto-settings", { skills_score: threshold });
    skillsDirty = false;
    if (next.sources) applyState(next);
    else await refresh();
    showFeedback("Порог навыков сохранён · " + threshold + "%.");
  } catch (error) {
    showError(error.message);
  } finally {
    save.disabled = field.readOnly = false;
  }
});

byId("hh-resume-url-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = byId("save-hh-resume-url");
  if (button.disabled) return;
  showHHResumeUrlError();
  button.disabled = true;
  button.textContent = "Проверяю резюме…";
  hhResumeUrlField.readOnly = true;
  try {
    const next = await api("hh-resumes", { url: hhResumeUrlField.value.trim() });
    if (hhResumeUrlDialog.open) hhResumeUrlDialog.close();
    showHHResumeBound(next);
  } catch (error) {
    if (hhResumeUrlDialog.open) showHHResumeUrlError(error.message);
    else showError(error.message);
  } finally {
    hhResumeUrlField.readOnly = false;
    button.disabled = false;
    button.textContent = "Использовать резюме";
  }
});
hhResumeUrlField.addEventListener("input", () => showHHResumeUrlError());

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
byId("journal-source").addEventListener("change", event => {
  journalSource = event.target.value;
  renderJobs();
});
byId("hh-activity-settings").addEventListener("click", async () => {
  await changeTab("connections");
  if (!byId("hh-auto-card").hidden) dataAccessSection("hh-auto-title");
});
byId("hh-activity-check").addEventListener("click", () => {
  if (selectedFilter === "sent") byId("jobs-filters").querySelector('[data-filter="draft"]').click();
  byId("jobs").querySelector("[data-hh-reconcile]")?.focus();
});
byId("reload").addEventListener("click", () => {
  if (!auth) return;
  if (activeSearch && selectedTab === "search") {
    activeSearch.failures = 0;
    pollSearch(activeSearch);
  } else refresh(!state).catch(error => showError(error.message));
});
byId("close-hh").addEventListener("click", () => hhDialog.close());
byId("close-hh-resume-url").addEventListener("click", () => hhResumeUrlDialog.close());
byId("close-job").addEventListener("click", () => byId("job-dialog").close());
hhResumeUrlDialog.addEventListener("close", () => byId("hh-resume-url-open")?.focus());
hhDialog.addEventListener("close", async () => {
  const selecting = hhDialog.dataset.mode === "resume";
  const editing = hhDialog.dataset.mode === "edit";
  hhFrame.removeAttribute("src");
  try {
    if (editing) {
      hhWindow = undefined;
      await api("cancel", {});
    }
    await refresh();
  } catch (error) {
    showError(error.message);
  }
  const target = byId("hh-continue") || byId("hh-cancel") ||
    (editing ? byId("hh-editor-title") : selecting ? byId("hh-find-resume") : byId("hh-connect") || byId("hh-switch"));
  target?.focus();
});
window.addEventListener("message", event => {
  if (event.origin !== location.origin || event.source !== hhFrame.contentWindow || !hhDialog.open) return;
  const selecting = hhDialog.dataset.mode === "resume";
  if (event.data?.hhLogin === "done") {
    hhWindow = undefined;
    if (selecting) profileDirty = false;
    hhDialog.close();
    showFeedback(selecting ? hhResumeBoundMessage : "Вход в HH подтверждён. Сессия сохранена.");
  } else if (["blocked", "failed", "expired"].includes(event.data?.hhLogin)) {
    hhWindow = undefined;
    hhDialog.close();
    showError(event.data.hhLogin === "expired" ? "Срок ссылки HH истёк. Откройте окно заново." :
      selecting ? "Загрузка резюме HH не завершилась. Проверьте статус аккаунта." :
        "Вход в HH не завершился. Проверьте статус аккаунта.");
  }
});
document.addEventListener("visibilitychange", () => {
  if (!state || document.visibilityState !== "visible") return;
  if (activeSearch && selectedTab === "search") pollSearch(activeSearch);
  if (selectedTab === "connections") refresh(false, true).catch(() => {});
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
