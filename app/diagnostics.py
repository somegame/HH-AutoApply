import os
import platform
import shutil
from pathlib import Path

from app.config import VERSION


def number(path):
    try:
        return int(path.read_text())
    except (OSError, ValueError):
        return None


def memory():
    try:
        groups = {}
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            _, controllers, path = line.split(":", 2)
            for controller in controllers.split(","):
                groups[controller] = Path(path)
        mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    except (OSError, ValueError):
        return None, None
    for mount in mounts:
        before, after = mount.split(" - ", 1)
        fs, _, options = after.split()
        if fs == "cgroup2":
            group = groups.get("")
            files = ("memory.current", "memory.max")
        elif fs == "cgroup" and "memory" in options.split(","):
            group = groups.get("memory")
            files = ("memory.usage_in_bytes", "memory.limit_in_bytes")
        else:
            continue
        if group is None:
            continue
        root, point = map(Path, before.split()[3:5])
        directory = point / (
            group.relative_to(root) if group.is_relative_to(root) else group.relative_to("/")
        )
        try:
            # A shared root may be mounted without exposing this process's actual cgroup.
            with (directory / "cgroup.procs").open() as members:
                visible = False
                for line in members:
                    pid = line.strip()
                    if pid == "0":
                        visible = False
                        break
                    if pid == str(os.getpid()):
                        visible = True
                if not visible:
                    continue
        except OSError:
            continue
        used, limit = [number(directory / name) for name in files]
        if used is not None:
            return used, limit if limit and limit < 1 << 60 else None
    return None, None


def process_memory():
    try:
        return next(
            int(line.split()[1]) * 1024
            for line in Path("/proc/self/status").read_text().splitlines()
            if line.startswith("VmRSS:")
        )
    except (OSError, StopIteration, IndexError, ValueError):
        return None


def mib(value):
    return f"{value / 1024**2:.1f} МиБ" if value is not None else "не определено"


def resource_warning(directory):
    used, limit = memory()
    if used is not None and limit and used >= limit * 0.85:
        return "RAM контейнера занято на 85% или больше."
    if shutil.disk_usage(directory).free < 25 * 1024**2:
        return "На файловой системе DATA_DIR осталось меньше 25 МиБ."
    return None


def report(config, db):
    used, limit = memory()
    available = shutil.disk_usage(config.data_dir).free
    with db.connection() as connection:
        healthy = connection.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
    storage = (
        "Указано владельцем как постоянное"
        if config.persistent
        else "Предупреждение — постоянство не подтверждено"
    )
    return "\n".join(
        (
            f"HH AutoApply {VERSION} · Python {platform.python_version()} · {platform.system()}",
            f"RAM процесса (RSS) — {mib(process_memory())}",
            f"RAM контейнера (cgroups) — {mib(used)} / {mib(limit)}",
            f"Свободно на файловой системе DATA_DIR — {mib(available)}",
            f"Доступ процесса на запись в DATA_DIR — {'есть' if os.access(config.data_dir, os.W_OK) else 'ошибка'}",
            f"Хранилище — {storage}. Проверьте сохранность после повторного деплоя.",
            f"SQLite — {'работает' if healthy else 'ошибка'}",
            "Telegram — работает, получен запрос владельца в личном чате.",
            "HH, авторизация и CDP — не проверялись. Вход и отклик выполняются на вашем iPhone.",
        )
    )
