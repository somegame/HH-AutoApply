import subprocess

tracked = subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0")
paths = b"\0".join(path for path in tracked if path) + b"\0"
check = subprocess.run(
    ["git", "check-ignore", "--no-index", "-z", "--stdin"],
    input=paths,
    capture_output=True,
    check=False,
)
if check.returncode != 1:
    raise SystemExit("Обнаружены файлы под правилами .gitignore или проверка Git не удалась.")
