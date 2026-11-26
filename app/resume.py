import asyncio
import resource
import sys
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

MAX_FILE = 2 * 1024 * 1024
MAX_TEXT = 24_000
DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def checked(text):
    text = text.strip()
    if len(text) > MAX_TEXT or sum(char.isalpha() for char in text) < 60:
        raise ValueError("Резюме должно содержать от 60 букв до 24 000 символов.")
    return text


def extract(data, suffix):
    if suffix == ".txt":
        try:
            return checked(data.decode("utf-8-sig"))
        except UnicodeDecodeError:
            return checked(data.decode("cp1251"))
    if suffix == ".docx":
        with ZipFile(BytesIO(data)) as archive:
            entry = archive.getinfo("word/document.xml")
            if entry.file_size > MAX_FILE:
                raise ValueError("Содержимое DOCX слишком большое.")
            doc = ElementTree.fromstring(archive.read(entry))
        paragraphs = doc.iter(DOCX_NS + "p")
        return checked(
            "\n".join(
                "".join(node.text or "" for node in p.iter(DOCX_NS + "t")) for p in paragraphs
            )
        )
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted or len(reader.pages) > 20:
            raise ValueError("Нужен PDF без пароля, максимум 20 страниц.")
        return checked("\n".join(page.extract_text() for page in reader.pages))
    raise ValueError("Поддерживаются TXT, DOCX и PDF с текстовым слоем.")


async def read_upload(filename, data):
    if not 0 < len(data) <= MAX_FILE:
        raise ValueError("Максимальный размер файла — 2 МБ.")
    suffix = Path(filename).suffix.lower()
    if suffix not in {".txt", ".docx", ".pdf"}:
        raise ValueError("Поддерживаются TXT, DOCX и PDF с текстовым слоем.")
    if suffix == ".txt":
        try:
            return extract(data, suffix)
        except UnicodeError:
            raise ValueError("Сохраните TXT в кодировке UTF-8.") from None
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.resume",
        suffix,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(worker.communicate(data), 10)
    finally:
        if worker.returncode is None:
            try:
                worker.kill()
            except ProcessLookupError:
                pass
            await worker.wait()
    if worker.returncode or not output:
        raise ValueError("Не удалось извлечь текст. Для PDF нужен текстовый слой. Попробуйте TXT.")
    return checked(output.decode("utf-8"))


def worker_main():
    resource.setrlimit(resource.RLIMIT_AS, (96 * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    data = sys.stdin.buffer.read(MAX_FILE + 1)
    if 0 < len(data) <= MAX_FILE:
        sys.stdout.buffer.write(extract(data, sys.argv[1]).encode("utf-8"))


if __name__ == "__main__":
    worker_main()
