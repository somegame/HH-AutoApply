"use strict";

function vacancyDiscovery({ byId, el, api, action, providerName, makeCard, getPreferences, updateState, showError, showFeedback }) {
  const compareIds = new Set();
  const sort = byId("result-sort");
  const compareDialog = byId("compare-dialog");
  const mapPanel = byId("job-map-panel");
  const mapNode = byId("job-map");
  const travelForm = byId("travel-form");
  let jobs = [];
  let currentView = "list";
  let leafletMap;
  let officeLayer;
  let officeId;
  let previousMapKey;
  let tileFailed = false;
  let travelDirty = false;
  let previousPreferences;
  const renderedCards = new WeakMap();

  function percent(value) {
    return Number.isFinite(value) ? Math.round(value) : null;
  }

  function namedMetric(label, value, suffix = "") {
    const container = el("div", "", "named-metric");
    container.append(el("strong", value === null ? "—" : String(value) + suffix), el("span", label));
    return container;
  }

  function address(job) {
    return job.address || job.location?.address || "";
  }

  function officePoint(job) {
    const { lat, lon } = job.location || {};
    return Number.isFinite(lat) && Math.abs(lat) <= 90 && Number.isFinite(lon) && Math.abs(lon) <= 180
      ? [lat, lon]
      : null;
  }

  function commuteLabel(job) {
    const trip = job.commute;
    if (trip?.kind === "remote") return "Удалённо · без поездок";
    if (!Number.isFinite(trip?.minutes)) return "Дорога пока не рассчитана";
    if (trip.kind === "routed") return trip.minutes + " мин по маршруту";
    if (Number.isFinite(trip.min_minutes) && Number.isFinite(trip.max_minutes) &&
        trip.min_minutes !== trip.max_minutes) {
      return trip.min_minutes + "–" + trip.max_minutes + " мин · оценка";
    }
    return "Около " + trip.minutes + " мин";
  }

  function formatPublished(published) {
    if (typeof published !== "string" || !/^\d{4}-\d{2}-\d{2}/.test(published)) return "";
    const date = new Date(published);
    return Number.isNaN(date.getTime()) ? "" :
      new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeZone: "UTC" }).format(date);
  }

  function assessment(job) {
    const rank = job.rank;
    const fit = job.analysis_status === "complete" ? percent(job.score) : percent(rank?.fit);
    const skills = job.analysis_status === "complete" ? percent(job.skills_score) : null;
    const rankedProvider = providerName(rank?.fit_kind);
    const actual = job.analysis_status === "complete" || (!job.analysis_status && Boolean(rankedProvider));
    const provider = providerName(job.analysis_provider) || rankedProvider || "Groq";
    const analysisNote = {
      queued: "Ожидает анализа",
      retry: "Анализ будет повторён",
      filtered: "За пределами фильтров поиска",
      error: "Анализ не завершился",
      unavailable: "Оценка пока недоступна",
    }[job.analysis_status];
    if (!rank && !job.commute && !address(job) && !job.format && !job.published &&
        fit === null && skills === null && !analysisNote) return null;
    const block = el("div", "", "discovery-assessment");
    const metrics = el("div", "", "job-metrics");
    if (fit !== null) metrics.append(namedMetric(actual ? "Оценка " + provider : "Предв. оценка", fit, "%"));
    if (skills !== null) metrics.append(namedMetric("Навыки · " + provider, skills, "%"));
    if (percent(rank?.score) !== null) metrics.append(namedMetric("Рейтинг факторов", percent(rank.score), " / 100"));
    if (metrics.childElementCount) block.append(metrics);
    if (analysisNote) block.append(el("p", analysisNote, "analysis-note"));
    if (rank?.missing_required) {
      block.append(el("p", "Неподтверждённые обязательные требования: " + rank.missing_required, "muted"));
    }
    const facts = el("div", "", "job-facts");
    const published = formatPublished(job.published);
    if (rank || job.commute) facts.append(el("span", commuteLabel(job), job.commute ? "trip-chip" : "muted"));
    if (job.format) facts.append(el("span", job.format, "muted"));
    if (published) facts.append(el("span", published, "muted"));
    if (facts.childElementCount) block.append(facts);
    if (address(job)) block.append(el("p", address(job), "muted"));
    if (Array.isArray(rank?.factors) && rank.factors.length) {
      const factors = el("details", "", "job-factors");
      factors.append(el("summary", "Из чего сложился балл"));
      if (Number.isFinite(rank.coverage)) {
        factors.append(el("p", "Полнота данных " + percent(rank.coverage) + "%", "muted"));
      }
      const breakdown = el("div", "", "factor-breakdown");
      for (const factor of rank.factors) {
        const item = el("div", "", "factor");
        const heading = el("div", "", "factor-heading");
        const score = percent(factor.score);
        heading.append(el("strong", factor.label), el("span", score === null ? "Нет данных" : score + " / 100", "muted"));
        item.append(heading);
        if (score !== null) {
          const meter = el("meter", "", "factor-meter");
          meter.min = 0;
          meter.max = 100;
          meter.value = score;
          meter.setAttribute("aria-label", factor.label);
          item.append(meter);
        }
        if (factor.detail) item.append(el("p", factor.detail, "muted"));
        breakdown.append(item);
      }
      factors.append(breakdown);
      block.append(factors);
    }
    return block;
  }

  function compareButton(job) {
    const button = el("button", "", "secondary compare-select");
    button.type = "button";
    button.dataset.compareId = String(job.id);
    button.addEventListener("click", () => {
      const id = button.dataset.compareId;
      if (compareIds.has(id)) compareIds.delete(id);
      else if (compareIds.size < 3) compareIds.add(id);
      else {
        showError("Для сравнения можно выбрать до трёх вакансий. Снимите одну отметку.");
        return;
      }
      showError();
      syncCompare();
    });
    return button;
  }

  async function calculateRoute(job) {
    const updated = await api("route", { id: String(job.id) });
    for (const key of ["commute", "location", "rank", "top"]) {
      if (Object.hasOwn(updated, key)) job[key] = updated[key];
    }
    let top = 0;
    for (const candidate of sortedJobs("best")) {
      const rank = candidate.rank || {};
      candidate.top = top < 3 && [null, "queued", "draft", "approved", "deferred"].includes(candidate.status ?? null)
        && rank.score >= 75 && rank.coverage >= 60 && !rank.missing_required;
      if (candidate.top) top += 1;
    }
    render(jobs);
    if (updated.commute?.kind === "routed") showFeedback("Время по маршруту обновлено.");
    else if (updated.commute) showFeedback("Для этой поездки доступна приблизительная оценка.");
    else if (updated.location) showFeedback("Офис добавлен на карту.");
    else showFeedback("Площадка пока не подтвердила точку работы для расчёта.");
  }

  function routeControls(job) {
    const preferences = getPreferences();
    const transit = preferences.travel_mode === "transit";
    const hasOffice = Boolean(officePoint(job));
    const onlyLocate = !preferences.home || transit;
    if (!preferences.route_provider || job.commute?.kind === "remote" ||
        (!hasOffice && !address(job)) || (onlyLocate && hasOffice)) return null;
    const details = el("details", "", "job-route");
    details.append(el("summary", onlyLocate ? "Найти офис на карте" : "Уточнить время по маршруту"),
      el("p", onlyLocate
        ? "В OpenRouteService отправится опубликованный адрес офиса."
        : "В OpenRouteService отправятся точка отправления и данные офиса. Время рассчитывается без учёта пробок.",
        "muted"),
      action(onlyLocate ? "Уточнить адрес офиса" : "Рассчитать маршрут", () => calculateRoute(job)));
    return details;
  }

  function fillInsights() {
    const best = jobs.filter(job => job.top).length;
    const mapped = jobs.filter(job => officePoint(job)).length;
    const timed = jobs.filter(job => Number.isFinite(job.commute?.minutes)).length;
    byId("search-insights").replaceChildren(
      namedMetric("Лучшие варианты", best),
      namedMetric("Есть на карте", mapped),
      namedMetric("Дорога оценена", timed),
    );
  }

  function sortedJobs(order = sort.value) {
    const number = (value, fallback) => Number.isFinite(value) ? value : fallback;
    const descending = (a, b) => number(b, -1) - number(a, -1);
    const best = (a, b) => descending(a.rank?.priority ?? a.rank?.score, b.rank?.priority ?? b.rank?.score)
      || descending(a.rank?.coverage, b.rank?.coverage);
    const commute = (job) => number(job.commute?.minutes, Number.MAX_SAFE_INTEGER);
    const salary = (job) => Object.hasOwn(job, "salary_sort") ? job.salary_sort : job.salary_max;
    const fresh = (job) => Date.parse(job.published || "");
    const comparisons = {
      best,
      fit: (a, b) => descending(a.rank?.fit, b.rank?.fit),
      salary: (a, b) => descending(salary(a), salary(b)),
      commute: (a, b) => commute(a) - commute(b),
      fresh: (a, b) => number(fresh(b), 0) - number(fresh(a), 0),
    };
    return [...jobs].sort((a, b) => (comparisons[order] || best)(a, b) || best(a, b));
  }

  function drawOffice(job) {
    officeId = job?.id;
    const body = byId("job-map-office");
    body.replaceChildren();
    if (job) body.append(makeCard(job, false));
    syncCompare();
  }

  function renderMap(ordered) {
    const offices = ordered.flatMap(job => {
      const point = officePoint(job);
      return point ? [{ job, point }] : [];
    });
    const status = byId("job-map-note");
    const count = "Адреса с координатами: " + offices.length + " из " + jobs.length + ".";
    mapNode.hidden = !offices.length;
    if (!offices.length) {
      status.textContent = "У найденных вакансий нет координат. Адреса, если они опубликованы, доступны в списке.";
      drawOffice();
      return;
    }
    if (!window.L) {
      status.textContent = "Карта пока недоступна. Переключитесь на список, чтобы посмотреть адреса.";
      mapNode.hidden = true;
      drawOffice();
      return;
    }
    status.textContent = count + (tileFailed ? " Фон карты недоступен, метки остаются видны." : "");
    if (!leafletMap) {
      const L = window.L;
      leafletMap = L.map(mapNode, { scrollWheelZoom: false, maxZoom: 18, zoomAnimation: false });
      leafletMap.attributionControl.setPrefix(false);
      const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 18,
        referrerPolicy: "strict-origin-when-cross-origin",
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>',
      });
      tiles.on("tileerror", () => {
        tileFailed = true;
        if (!mapPanel.hidden) status.textContent = "Фон карты недоступен, метки остаются видны.";
      });
      tiles.addTo(leafletMap);
      officeLayer = L.layerGroup().addTo(leafletMap);
    }
    const key = JSON.stringify(offices.map(item => [item.job.id, item.point, Boolean(item.job.top)]));
    leafletMap.invalidateSize({ animate: false, pan: false });
    if (previousMapKey !== key) {
      previousMapKey = key;
      officeLayer.clearLayers();
      offices.forEach(({ job, point }, index) => {
        const icon = window.L.divIcon({
          className: "office-pin" + (job.top ? " office-pin-top" : ""),
          html: "<span aria-hidden=\"true\">" + (index + 1) + "</span>",
          iconSize: [38, 38],
          iconAnchor: [19, 19],
        });
        const marker = window.L.marker(point, { icon, keyboard: true, title: job.title, alt: job.title });
        marker.on("click", () => drawOffice(job));
        marker.addTo(officeLayer);
        marker.getElement()?.setAttribute("aria-label", job.title);
      });
      if (offices.length === 1) leafletMap.setView(offices[0].point, 14);
      else leafletMap.fitBounds(offices.map(item => item.point), { padding: [24, 28], maxZoom: 15, animate: false });
    }
    drawOffice((offices.find(item => item.job.id === officeId) || offices[0]).job);
  }

  function renderList(ordered) {
    const results = byId("results");
    const existing = new Map(Array.from(results.children, card => [card.dataset.jobId, card]));
    for (const [index, job] of ordered.entries()) {
      const id = String(job.id);
      const previous = existing.get(id);
      existing.delete(id);
      const fingerprint = JSON.stringify([job, getPreferences()]);
      let card = previous;
      if (!previous || renderedCards.get(previous) !== fingerprint) {
        card = makeCard(job, false);
        renderedCards.set(card, fingerprint);
        if (previous) {
          const opened = new Set(Array.from(previous.querySelectorAll("details[open]"), item => item.className));
          for (const details of card.querySelectorAll("details")) details.open = opened.has(details.className);
          previous.replaceWith(card);
        }
      }
      if (results.children[index] !== card) results.insertBefore(card, results.children[index] || null);
    }
    for (const card of existing.values()) card.remove();
  }

  function render(list) {
    jobs = Array.isArray(list) ? list : [];
    byId("discovery-tools").hidden = !jobs.length;
    for (const id of compareIds) {
      if (!jobs.some(job => String(job.id) === id)) compareIds.delete(id);
    }
    const ordered = sortedJobs();
    byId("list-view").setAttribute("aria-pressed", String(currentView === "list"));
    byId("map-view").setAttribute("aria-pressed", String(currentView === "map"));
    mapPanel.hidden = currentView !== "map" || !jobs.length;
    byId("results").hidden = !mapPanel.hidden;
    if (!mapPanel.hidden) renderMap(ordered);
    else renderList(ordered);
    fillInsights();
    syncCompare();
  }

  function renderCompareDialog() {
    const cards = jobs.filter(job => compareIds.has(String(job.id))).map(job => {
      const article = el("article", "", "compare-card");
      article.append(el("h3", job.title), el("p", job.company, "muted"),
        el("p", job.salary || "Зарплата не указана", "salary"));
      const stats = assessment(job);
      if (stats) article.append(stats);
      else article.append(el("p", "Подробные факторы пока недоступны.", "muted"));
      return article;
    });
    byId("compare-body").replaceChildren(...cards);
  }

  function syncCompare() {
    for (const button of document.querySelectorAll(".compare-select")) {
      const selected = compareIds.has(button.dataset.compareId);
      button.setAttribute("aria-pressed", String(selected));
      button.textContent = selected ? "В сравнении" : "К сравнению";
    }
    byId("compare-toolbar").hidden = !compareIds.size;
    byId("compare-count").textContent = compareIds.size < 2
      ? "Выберите ещё одну вакансию"
      : "Выбрано " + compareIds.size + " из 3";
    byId("compare-open").disabled = compareIds.size < 2;
  }

  function prepPreferences(preferences = {}, first = false) {
    if (!travelDirty && JSON.stringify(preferences) !== previousPreferences) {
      const point = preferences.home || {};
      byId("travel-lat").value = point.lat ?? "";
      byId("travel-lon").value = point.lon ?? "";
      byId("travel-mode").value = preferences.travel_mode || "transit";
      byId("travel-cap").value = preferences.max_commute ?? 0;
      byId("travel-format").value = preferences.preferred_format || "any";
      byId("travel-status").textContent = preferences.home
        ? "Точка отправления сохранена."
        : "Добавьте точку, чтобы увидеть время до офиса.";
      byId("travel-locate").hidden = !preferences.route_provider;
      previousPreferences = JSON.stringify(preferences);
      updateClearButton();
    }
    if (first) {
      byId("search-commute").value = preferences.max_commute ?? 0;
      byId("search-format").value = preferences.preferred_format || "any";
    }
  }

  function updateClearButton() {
    byId("travel-clear").hidden = !byId("travel-lat").value && !byId("travel-lon").value;
  }

  function decimal(value, max) {
    const raw = value.trim().replace(",", ".");
    if (!/^[-+]?\d+(?:\.\d+)?$/.test(raw) || Math.abs(Number(raw)) > max) {
      throw new Error("Проверьте широту и долготу точки отправления.");
    }
    return Number(raw);
  }

  function previewPoint(point) {
    byId("travel-lat").value = point.lat.toFixed(5);
    byId("travel-lon").value = point.lon.toFixed(5);
    travelDirty = true;
    updateClearButton();
    byId("travel-status").textContent = "Проверьте координаты и сохраните настройки.";
  }

  function setBusy(button, busy) {
    button.disabled = busy;
    if (busy) showError();
  }

  byId("list-view").addEventListener("click", () => { currentView = "list"; render(jobs); });
  byId("map-view").addEventListener("click", () => { currentView = "map"; render(jobs); });
  sort.addEventListener("change", () => render(jobs));
  byId("compare-open").addEventListener("click", () => {
    renderCompareDialog();
    compareDialog.showModal();
  });
  byId("compare-close").addEventListener("click", () => compareDialog.close());
  compareDialog.addEventListener("close", () => byId("compare-open").focus());

  travelForm.addEventListener("input", event => {
    if (event.target.id !== "travel-address") travelDirty = true;
    updateClearButton();
  });
  travelForm.addEventListener("submit", async event => {
    event.preventDefault();
    const button = event.submitter;
    setBusy(button, true);
    try {
      const lat = byId("travel-lat").value.trim();
      const lon = byId("travel-lon").value.trim();
      const max_commute = Number(byId("travel-cap").value);
      const preferred_format = byId("travel-format").value;
      const { preferences } = await api("preferences", {
        home: lat || lon ? { lat: decimal(lat, 90), lon: decimal(lon, 180) } : null,
        travel_mode: byId("travel-mode").value,
        preferred_format,
        max_commute,
      });
      travelDirty = false;
      previousPreferences = undefined;
      updateState(preferences);
      byId("search-commute").value = String(max_commute);
      byId("search-format").value = preferred_format;
      showFeedback("Настройки дороги сохранены. Они применятся к следующему поиску.");
    } catch (error) {
      showError(error.message);
    } finally {
      setBusy(button, false);
    }
  });
  byId("travel-clear").addEventListener("click", () => {
    byId("travel-lat").value = byId("travel-lon").value = "";
    travelDirty = true;
    updateClearButton();
    byId("travel-status").textContent = "Точка будет удалена после сохранения настроек.";
  });
  if (navigator.geolocation) {
    const button = byId("travel-position");
    button.hidden = false;
    button.addEventListener("click", () => {
      setBusy(button, true);
      navigator.geolocation.getCurrentPosition(
        ({ coords }) => {
          setBusy(button, false);
          previewPoint({ lat: coords.latitude, lon: coords.longitude });
        },
        () => {
          setBusy(button, false);
          showError("Не удалось получить геопозицию. Введите координаты или найдите адрес, если поиск доступен.");
        },
        { enableHighAccuracy: false, maximumAge: 60000, timeout: 8000 },
      );
    });
  }
  byId("travel-geocode").addEventListener("click", async event => {
    const field = byId("travel-address");
    const address = field.value.trim();
    if (!address) {
      showError("Введите адрес для поиска точки отправления.");
      field.focus();
      return;
    }
    setBusy(event.currentTarget, true);
    const button = event.currentTarget;
    try {
      const result = await api("locate", { address });
      if (!officePoint({ location: result.point })) throw new Error("Адрес не найден. Проверьте город и номер дома.");
      previewPoint(result.point);
    } catch (error) {
      showError(error.message);
    } finally {
      field.value = "";
      setBusy(button, false);
    }
  });

  return {
    assessment,
    compareButton,
    prepPreferences,
    render,
    routeControls,
    resultSort: () => sort.value,
  };
}
