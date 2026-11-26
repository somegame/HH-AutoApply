const token = location.hash.slice(1);
history.replaceState(null, "", location.pathname);
const screen = document.querySelector("#screen");
const status = document.querySelector("#status");
const value = document.querySelector("#value");
let ended = !/^[A-Za-z0-9_-]{43}$/.test(token);
let active = false;
let frameUrl;
const headers = { Authorization: `Bearer ${token}` };

function handle(response) {
  if (response.status === 401 || response.status === 403) {
    ended = true;
    screen.removeAttribute("src");
    if (frameUrl) URL.revokeObjectURL(frameUrl);
    status.textContent = "Ссылка закрыта или истекла. Вернитесь в Telegram.";
  } else if (response.status === 409) {
    status.textContent = "Страница HH недоступна или показала проверку. Вернитесь в Telegram.";
  } else if (response.status === 422) {
    status.textContent = "Сначала нажмите на поле на экране HH.";
  }
}

async function refresh() {
  if (ended) return;
  if (!active) {
    active = true;
    try {
      const response = await fetch("/hh-login/frame", { headers, cache: "no-store" });
      if (response.ok) {
        const next = URL.createObjectURL(await response.blob());
        screen.src = next;
        if (frameUrl) URL.revokeObjectURL(frameUrl);
        frameUrl = next;
        if (status.dataset.loaded !== "yes") status.textContent = "Браузер подключён.";
        status.dataset.loaded = "yes";
      } else handle(response);
    } catch {
      status.textContent = "Связь прервана. Пробую подключиться снова.";
    } finally {
      active = false;
    }
  }
  if (!ended) setTimeout(refresh, 650);
}

async function send(event) {
  if (ended) return;
  if (active) {
    setTimeout(() => send(event), 120);
    return;
  }
  active = true;
  try {
    const response = await fetch("/hh-login/input", {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify(event),
    });
    if (!response.ok) handle(response);
    else status.textContent = "Действие передано в браузер HH.";
  } catch {
    status.textContent = "Связь прервана. Повторите действие.";
  } finally {
    active = false;
  }
}

screen.addEventListener("click", event => {
  if (!frameUrl) return;
  const box = screen.getBoundingClientRect();
  send({ kind: "click", x: 430 * (event.clientX - box.left) / box.width,
    y: 820 * (event.clientY - box.top) / box.height });
});
document.querySelector("#entry").addEventListener("submit", event => {
  event.preventDefault();
  const text = value.value;
  value.value = "";
  if (text) send({ kind: "entry", text });
});
for (const direction of ["up", "down"]) {
  document.querySelector("#" + direction).addEventListener("click", () =>
    send({ kind: "scroll", direction }));
}
if (ended) status.textContent = "Откройте свежую ссылку из Telegram.";
else refresh();
