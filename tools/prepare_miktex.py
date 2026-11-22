"""Подготовить локальный репозиторий пакетов для уже установленного MiKTeX."""

import argparse
import hashlib
import io
import os
import re
import shutil
import sys
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


MIRROR = "https://ctan.math.illinois.edu/systems/win32/miktex/tm/packages/"
MANIFEST = Path(__file__).resolve().parents[1] / "resources" / "miktex-packages.txt"
DATABASES = tuple(f"miktex-zzdb{i}-2.9.tar.lzma" for i in (1, 2, 3))
EXTENSIONS = {"TarLzma": ".tar.lzma", "TarBzip2": ".tar.bz2", "TarXz": ".tar.xz", "MSCab": ".cab"}
LOCK = ".mdpdf-lock"
SNAPSHOT = ".mdpdf-snapshot"


class Source:
    def __init__(self, location: str, dest: Path):
        self.url = location.rstrip("/") + "/" if location.lower().startswith("https://") else None
        if self.url:
            parts = urlsplit(location)
            if not parts.hostname or parts.query or parts.fragment:
                raise ValueError("Укажите HTTPS-адрес каталога пакетов одного зеркала.")
        else:
            self.path = Path(location).resolve(strict=True)
            if not self.path.is_dir():
                raise ValueError(f"Ожидается каталог репозитория: {self.path}")
            if self.path.is_relative_to(dest) or dest.is_relative_to(self.path):
                raise ValueError("Исходный и выходной каталоги должны быть раздельными.")

    def fetch(self, name: str, output: Path):
        if not self.url:
            shutil.copyfile(self.path / name, output)
            return
        request = Request(self.url + quote(name), headers={"User-Agent": "md-pdf-pipeline"})
        for attempt in range(3):
            try:
                with urlopen(request, timeout=60) as src, output.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                return
            except HTTPError as exc:
                exc.close()
                if exc.code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                    raise OSError(f"Не удалось получить {name}: {exc}") from exc
            except (URLError, HTTPException, TimeoutError, ConnectionError) as exc:
                if attempt == 2:
                    raise OSError(f"Не удалось получить {name}: {exc}") from exc


def records(archive: Path, member: str, keys: set[str]) -> dict:
    data = {}
    with tarfile.open(archive, "r:*") as db:
        with io.TextIOWrapper(db.extractfile(member), encoding="utf-8") as stream:
            record = None
            for line in stream:
                line = line.rstrip("\r\n")
                if line.startswith("[") and line.endswith("]"):
                    record = data[line[1:-1]] = {}
                elif record is not None:
                    key, _, value = line.partition("=")
                    if key in keys:
                        if key == "require[]":
                            record.setdefault(key, []).append(value)
                        else:
                            record[key] = value
    return data


def requested_packages(path: Path) -> list[str]:
    lines = (line.partition("#")[0].strip() for line in path.read_text(encoding="utf-8").splitlines())
    ids = [line for line in lines if line]
    if not ids:
        raise ValueError(f"В списке нет идентификаторов пакетов: {path}")
    return ids


def packages(stage: Path, ids: list[str]) -> dict[str, tuple[int, str]]:
    archives = records(stage / DATABASES[0], "mpm.ini", {"Type", "CabMD5", "CabSize", "MD5"})
    manifests = records(stage / DATABASES[2], "package-manifests.ini", {"digest", "require[]"})
    result, visited, pending = {}, set(), ids.copy()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_+.\-]*", name) or name not in manifests:
            raise ValueError(f"В манифесте MiKTeX нет пакета «{name}».")
        visited.add(name)
        manifest = manifests[name]
        deps = manifest.get("require[]", [])
        pending.extend(deps)
        if name not in archives and deps:
            continue  # Группа пакетов без собственного архива.
        entry = archives.get(name, {})
        extension = EXTENSIONS.get(entry.get("Type"))
        if not extension or not re.fullmatch(r"[a-fA-F0-9]{32}", entry.get("CabMD5", "")):
            raise ValueError(f"Для пакета «{name}» отсутствует описание поддерживаемого архива.")
        if entry["MD5"].casefold() != manifest["digest"].casefold():
            raise ValueError(f"Базы зеркала относятся к разным снимкам: {name}")
        result[name + extension] = int(entry["CabSize"]), entry["CabMD5"].casefold()
    return result


def digest(path: Path, algorithm: str) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).hexdigest()


def valid(path: Path, expected: tuple[int, str]) -> bool:
    return path.is_file() and path.stat().st_size == expected[0] and digest(path, "md5") == expected[1]


@contextmanager
def exclusive(dest: Path):
    with (dest / LOCK).open("a+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(f"Другой процесс уже готовит репозиторий в {dest}") from exc
        yield


def prepare(location: str, dest: Path, manifest: Path):
    dest = dest.resolve()
    source = Source(location, dest)
    ids = requested_packages(manifest)
    dest.mkdir(parents=True, exist_ok=True)
    if not (dest / SNAPSHOT).is_file() and any(p.name != LOCK for p in dest.iterdir()):
        raise ValueError(f"Выходной каталог уже содержит посторонние файлы: {dest}")
    with exclusive(dest), tempfile.TemporaryDirectory(prefix="mdpdf-miktex-", dir=dest.parent) as tmp:
        stage = Path(tmp)
        for name in ("pr.ini", *DATABASES):
            source.fetch(name, stage / name)
        snapshot = "\n".join(digest(stage / name, "sha256") for name in ("pr.ini", *DATABASES))
        marker = dest / SNAPSHOT
        if marker.exists():
            if marker.read_text(encoding="utf-8") != snapshot:
                raise ValueError("Снимок источника изменился. Укажите новый каталог --dest.")
        elif any(p.name != LOCK for p in dest.iterdir()):
            raise ValueError(f"Выходной каталог уже содержит посторонние файлы: {dest}")

        selected = packages(stage, ids)
        print(f"Пакетов для проверки и подготовки: {len(selected)}", flush=True)

        def fetch_package(item):
            name, expected = item
            if valid(dest / name, expected):
                return
            source.fetch(name, stage / name)
            if not valid(stage / name, expected):
                raise ValueError(f"Размер или контрольная сумма архива не совпали: {name}")

        with ThreadPoolExecutor(max_workers=6) as pool:
            for _ in pool.map(fetch_package, selected.items()):
                pass
        source.fetch("pr.ini", stage / "pr.check")
        if (stage / "pr.check").read_bytes() != (stage / "pr.ini").read_bytes():
            raise ValueError("Снимок источника изменился во время загрузки. Повторите подготовку.")

        # Маркер позволяет продолжить публикацию того же снимка после прерывания.
        if not marker.exists():
            (stage / SNAPSHOT).write_text(snapshot, encoding="utf-8")
            (stage / SNAPSHOT).replace(marker)
        for name in (*selected, *DATABASES, "pr.ini"):
            path = stage / name
            if path.is_file():
                path.replace(dest / name)
    print(f"Локальный репозиторий MiKTeX: {dest}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=MIRROR, help="каталог полного репозитория или HTTPS-адрес зеркала")
    parser.add_argument("--dest", type=Path, required=True, help="отдельный каталог для подготовленных пакетов")
    parser.add_argument("--manifest", type=Path, default=MANIFEST, help="файл UTF-8 с идентификаторами пакетов")
    args = parser.parse_args()
    try:
        prepare(args.source, args.dest, args.manifest)
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        print(f"Ошибка подготовки репозитория: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
