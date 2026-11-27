const token = location.hash.slice(1);
const resumeMode = new URLSearchParams(location.search).get("mode") === "resume";
history.replaceState(null, "", location.pathname);
const stage = document.querySelector("#stage");
const screen = document.querySelector("#screen");
const statusLine = document.querySelector("#status");
const check = document.querySelector("#check");
const fields = document.querySelector("#fields");
const smsForm = document.querySelector("#sms-form");
const smsCode = document.querySelector("#sms-code");
const smsSubmit = document.querySelector("#sms-submit");
const headers = { Authorization: `Bearer ${token}` };
const controls = new Map();
const pending = new Map();
let revision = "";
let frameUrl;
let ended = !/^[A-Za-z0-9_-]{43}$/.test(token);
let requests = Promise.resolve();
let timer;
let gesture;

const closed = {
  done: resumeMode ? "Резюме HH загружено." : "Вход подтверждён. Сессия HH сохранена.",
  failed: resumeMode ? "Резюме не загрузилось. Проверьте сообщение в «Аккаунтах»." :
    "Вход не завершился. Проверьте статус аккаунта в Telegram.",
  blocked: resumeMode ? "HH ограничил доступ к резюме. Проверьте сообщение в «Аккаунтах»." :
    "HH остановил вход. Проверьте статус аккаунта в Telegram.",
  expired: resumeMode ? "Окно выбора закрылось. Откройте его заново из «Аккаунтов»." :
    "Время входа истекло. Откройте вход заново из списка аккаунтов.",
};
const messages = {
  login: resumeMode ? "Откройте профиль HH, затем своё резюме с кнопкой «Редактировать»." :
    "Войдите на странице HH. После открытия личного кабинета проверка начнётся автоматически.",
  checking: resumeMode ? "Проверяю резюме HH…" : "Проверяю вход в аккаунт HH…",
  retry: resumeMode ? "Откройте карточку своего резюме и нажмите «Загрузить резюме»." :
    "Аккаунт пока не подтверждён. Если вы уже вошли, нажмите «Проверить вход».",
};
if (resumeMode) {
  document.querySelector("header strong").textContent = "Выбор резюме";
  check.textContent = "Загрузить резюме";
}

function end(state) {
  if (window.parent !== window) window.parent.postMessage({ hhLogin: state }, location.origin);
  ended = true;
  clearTimeout(timer);
  pending.clear();
  controls.clear();
  fields.replaceChildren();
  if (frameUrl) URL.revokeObjectURL(frameUrl);
  screen.removeAttribute("src");
  smsCode.value = "";
  stage.hidden = check.hidden = smsForm.hidden = true;
  statusLine.textContent = closed[state];
}

function send(event) {
  requests = requests.then(async () => {
    if (ended) return;
    try {
      const response = await fetch("/hh-login/input", {
        method: "POST",
        headers: { ...headers, "Content-Type": "application/json" },
        body: JSON.stringify(event),
      });
      if (response.status === 401 || response.status === 403) end("expired");
      else if (event.kind === "sms") {
        if (response.ok) {
          smsCode.value = "";
          statusLine.textContent = "Код введён на странице HH.";
        } else {
          statusLine.textContent = "HH не принял ввод. Проверьте код и повторите.";
        }
      } else if (!response.ok) {
        statusLine.textContent = "Действие не принято. Коснитесь поля ещё раз.";
      }
    } catch {
      statusLine.textContent = "Связь прервалась. Повторите действие.";
    } finally {
      if (event.kind === "sms") smsSubmit.disabled = false;
    }
  });
}

function flush(except) {
  clearTimeout(timer);
  for (const [field, text] of pending) {
    if (field !== except) send({ kind: "entry", field, text });
  }
  pending.clear();
}

function showFields(metadata) {
  const visible = new Set();
  for (const item of metadata) {
    visible.add(item.id);
    let input = controls.get(item.id);
    if (!input) {
      input = document.createElement("input");
      input.type = item.type === "password" ? "password" : "text";
      input.inputMode = item.code || item.type === "number" ? "numeric" :
        ["email", "tel", "numeric", "decimal"].includes(item.mode) ? item.mode :
        ["email", "tel"].includes(item.type) ? item.type : "text";
      input.autocomplete = item.code ? "one-time-code" : "off";
      input.autocapitalize = "off";
      input.spellcheck = false;
      input.setAttribute("aria-label", item.code ? "Код входа" :
        item.type === "password" ? "Пароль" :
        item.type === "tel" ? "Телефон" : item.type === "email" ? "Электронная почта" : "Поле HH");
      input.addEventListener("input", () => {
        pending.set(item.id, input.value);
        clearTimeout(timer);
        timer = setTimeout(flush, 160);
      });
      input.addEventListener("blur", () => flush());
      input.addEventListener("keydown", event => {
        if (event.key !== "Enter" || event.isComposing) return;
        event.preventDefault();
        flush(item.id);
        send({ kind: "enter", field: item.id, text: input.value });
        input.blur();
      });
      controls.set(item.id, input);
      fields.append(input);
    }
    input.style.left = `${100 * item.x / 430}%`;
    input.style.top = `${100 * item.y / 820}%`;
    input.style.width = `${100 * item.w / 430}%`;
    input.style.height = `${100 * item.h / 820}%`;
  }
  for (const [id, input] of controls) {
    if (visible.has(id)) continue;
    pending.delete(id);
    input.remove();
    controls.delete(id);
  }
}

async function refresh() {
  while (!ended) {
    try {
      const response = await fetch(`/hh-login/frame?after=${encodeURIComponent(revision)}`, {
        headers, cache: "no-store",
      });
      if (response.status === 401 || response.status === 403) return end("expired");
      if (!response.ok) throw new Error();
      const state = response.headers.get("X-HH-Status");
      if (closed[state]) return end(state);
      revision = response.headers.get("X-HH-Revision") || "";
      if (messages[state]) statusLine.textContent = messages[state];
      check.disabled = state === "checking";
      if (response.status === 200) {
        const next = URL.createObjectURL(await response.blob());
        screen.src = next;
        if (frameUrl) URL.revokeObjectURL(frameUrl);
        frameUrl = next;
        const sms = response.headers.get("X-HH-Sms") === "1";
        stage.hidden = false;
        check.hidden = sms;
        if (sms && smsForm.hidden) {
          smsForm.hidden = false;
          smsForm.scrollIntoView({ block: "nearest" });
        } else if (!sms) smsForm.hidden = true;
        if (!sms) smsCode.value = "";
        showFields(sms ? [] : JSON.parse(response.headers.get("X-HH-Fields") || "[]"));
      }
      await new Promise(resolve => setTimeout(resolve, 100));
    } catch {
      statusLine.textContent = "Переподключаюсь к HH…";
      await new Promise(resolve => setTimeout(resolve, 750));
    }
  }
}

screen.addEventListener("pointerdown", event => {
  gesture = event.clientY;
  screen.setPointerCapture(event.pointerId);
});
screen.addEventListener("pointercancel", () => { gesture = undefined; });
screen.addEventListener("pointerup", event => {
  if (gesture === undefined) return;
  const box = screen.getBoundingClientRect();
  const distance = gesture - event.clientY;
  gesture = undefined;
  flush();
  if (fields.contains(document.activeElement)) document.activeElement.blur();
  if (Math.abs(distance) > 16) {
    send({ kind: "scroll", delta: Math.max(-820, Math.min(820, 430 * distance / box.width)) });
  } else {
    send({ kind: "click", x: 430 * (event.clientX - box.left) / box.width,
      y: 820 * (event.clientY - box.top) / box.height });
  }
});
screen.addEventListener("wheel", event => {
  event.preventDefault();
  send({ kind: "scroll", delta: Math.max(-820, Math.min(820, event.deltaY)) });
}, { passive: false });
check.addEventListener("click", () => {
  flush();
  statusLine.textContent = messages.checking;
  send({ kind: "check" });
});
smsCode.addEventListener("input", () => {
  smsCode.value = smsCode.value.replace(/[^0-9]/g, "").slice(0, 8);
});
smsForm.addEventListener("submit", event => {
  event.preventDefault();
  if (ended || smsSubmit.disabled || !/^[0-9]{4,8}$/.test(smsCode.value)) return;
  smsSubmit.disabled = true;
  send({ kind: "sms", text: smsCode.value });
});
if (ended) end("expired");
else refresh();
