"""Сборка Markdown-документа или дерева статей в PDF."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESOURCES = ROOT / "resources"
MIN_PANDOC = (3, 8, 2)


def run(command, *, cwd=None, data=None):
    process = subprocess.run(command, cwd=cwd, input=data, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    if process.returncode:
        detail = (process.stderr or process.stdout).decode("utf-8", errors="replace").strip()
        raise ValueError(f"{Path(command[0]).name} завершился с кодом {process.returncode}.\n{detail[-4000:]}")
    if process.stderr:
        print(process.stderr.decode("utf-8", errors="replace").rstrip(), file=sys.stderr)
    return process.stdout


def version(name):
    program = shutil.which(name)
    if not program:
        raise ValueError(f"Команда {name} не найдена в PATH. См. INSTALL-GUIDE.md.")
    banner = run([program, "--version"]).decode("utf-8", errors="replace").splitlines()[0]
    return program, banner


def environment():
    from importlib.metadata import version as package_version

    try:
        from PIL import Image
        import resvg_py
        Image.init()
        if not callable(resvg_py.svg_to_bytes):
            raise ImportError("resvg_py.svg_to_bytes")
    except (ImportError, OSError) as exc:
        raise ValueError(f"Проверьте окружение Python командой setup.ps1. {exc}") from exc

    pandoc, pandoc_banner = version("pandoc")
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", pandoc_banner)
    if not match or tuple(int(n or 0) for n in match.groups()) < MIN_PANDOC:
        raise ValueError(f"Требуется Pandoc 3.8.2 или новее. Обнаружено: {pandoc_banner}")
    latex, latex_banner = version("lualatex")
    print(f"Python {sys.version.split()[0]} | Pillow {package_version('Pillow')} | "
          f"resvg_py {package_version('resvg_py')}\n{pandoc_banner}\n{latex_banner}", flush=True)
    return pandoc, latex, "miktex" in latex_banner.lower()


def font_cache_unavailable(log):
    try:
        with log.open("rb") as stream:
            # При запуске движка TeX переносит сообщения на границе колонки.
            startup = b"".join(stream.read(8192).lower().split())
    except OSError:
        return False
    return (b"luaotfload|load:fatalerror" in startup
            and b"system:nowriteablecachepath" in startup)


def run_latex_pass(engine, work, number):
    log = work / "book.log"
    for attempt in range(3):
        try:
            run([*engine, "book.tex"], cwd=work)
            return
        except ValueError:
            if attempt == 2 or not font_cache_unavailable(log):
                raise
            retry = attempt + 1
            log.replace(work / f"book.pass-{number}.retry-{retry}.log")
            print(f"Кеш шрифтов luaotfload недоступен. Повтор прохода {number} "
                  f"через {retry} с.", file=sys.stderr, flush=True)
            time.sleep(retry)


def typeset(document, work, pandoc, latex, miktex, offline):
    cmd = [pandoc, "--from=json", "--to=latex", "--standalone", "--pdf-engine=lualatex",
           "--top-level-division=section", "--toc", "--toc-depth=3", "--number-sections",
           "--variable-json=lang:false",
           f"--lua-filter={RESOURCES / 'print.lua'}", f"--include-in-header={RESOURCES / 'layout.tex'}",
           "-V", "papersize=a4", "-V", "geometry:margin=20mm", "-o", "book.tex"]
    run(cmd, cwd=work, data=json.dumps(document, ensure_ascii=False).encode("utf-8"))
    engine = [latex, "-no-shell-escape", "-halt-on-error", "-file-line-error", "-interaction=nonstopmode"]
    if offline and miktex:
        engine.append("--disable-installer")
    previous = None
    for number in range(1, 6):
        print(f"LuaLaTeX, проход {number}", flush=True)
        run_latex_pass(engine, work, number)
        state = tuple(hashlib.sha256(p.read_bytes()).digest() if p.is_file() else None
                      for p in (work / "book.aux", work / "book.toc", work / "book.out"))
        if state == previous:
            return
        previous = state
    raise ValueError("За пять проходов LuaLaTeX номера страниц и ссылки не стабилизировались.")


def publish(pdf, output):
    with pdf.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError("LuaLaTeX не создал PDF.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".mdpdf-", suffix=".tmp", dir=output.parent,
                                     delete=False) as stage:
        path = Path(stage.name)
    try:
        shutil.copyfile(pdf, path)
        path.replace(output)
    finally:
        path.unlink(missing_ok=True)


def build(args, tools):
    from book import build_book
    from images import ImageCache

    source = args.source.resolve(strict=True)
    if args.output is None and not source.name:
        raise ValueError("Для корня диска укажите выходной PDF явно.")
    output = (args.output or (source.with_name(source.name + ".pdf") if source.is_dir()
                              else source.with_suffix(".pdf"))).resolve()
    if output.suffix.lower() != ".pdf" or output.is_dir():
        raise ValueError("Выходной путь должен указывать на файл .pdf.")
    cache_dir = args.cache_dir.expanduser().resolve()
    work = Path(tempfile.mkdtemp(prefix="md-pdf-"))
    try:
        pandoc, latex, miktex = tools
        images = ImageCache(cache_dir, args.offline, work_dir=work, pandoc=pandoc)
        document = build_book(source, pandoc, images.transform_media,
                              exclude_dirs=(cache_dir, work, ROOT / ".venv"))
        meta = document["meta"]
        if args.title:
            meta["title"] = {"t": "MetaString", "c": args.title}
        meta.setdefault("lang", {"t": "MetaString", "c": "ru"})
        typeset(document, work, pandoc, latex, miktex, args.offline)
        publish(work / "book.pdf", output)
    except BaseException:
        print(f"Рабочий каталог сборки: {work}", file=sys.stderr)
        raise
    shutil.rmtree(work)
    print(f"PDF: {output}")


def main():
    default_cache = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".cache")) / "md-pdf-pipeline" / "images"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, help="путь к статье .md, .markdown или папке")
    parser.add_argument("--output", type=Path, help="выходной файл .pdf")
    parser.add_argument("--title", help="название на титульной странице")
    parser.add_argument("--cache-dir", type=Path, default=default_cache, help="каталог кеша изображений")
    parser.add_argument("--offline", action="store_true", help="использовать кеш сетевых изображений")
    parser.add_argument("--check", action="store_true", help="проверить программы и модули")
    args = parser.parse_args()
    if not args.check and not args.source:
        parser.error("укажите путь к статье или папке")
    try:
        installed = environment()
        if not args.check:
            build(args, installed)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
