"""Сборка дерева статей из документов Pandoc с независимыми пространствами якорей."""

import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


OVERVIEWS = ("0-index.md", "index.md", "readme.md")
SKIP_DIRS = {"__pycache__", "node_modules", "venv"}
MARKDOWN_EXTENSIONS = {".md", ".markdown"}
ATTR_FIRST = {"Code", "CodeBlock", "Div", "Figure", "Image", "Link", "Span", "Table"}
HTML_SCRIPT = re.compile(r"<(/?)(sub|sup)\s*>", re.IGNORECASE)


@dataclass(frozen=True)
class Section:
    path: Path
    level: int
    overview_of: Path | None = None


def _name_key(path: Path) -> tuple:
    parts = re.split(r"([0-9]+)", path.name.casefold())
    return tuple(int(text) if i % 2 else text for i, text in enumerate(parts)), path.name


def _directory_plan(directory: Path, level: int, excluded: set[Path], *, root: bool = False) -> list[Section]:
    articles, children = [], []
    for path in sorted(directory.iterdir(), key=_name_key):
        if path.name.startswith(".") or path in excluded:
            continue
        info = path.lstat()
        attrs = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_REPARSE_POINT):
            continue
        if stat.S_ISDIR(info.st_mode) and path.name.casefold() not in SKIP_DIRS:
            children.extend(_directory_plan(path, level + 1, excluded))
        elif stat.S_ISREG(info.st_mode) and path.suffix.casefold() in MARKDOWN_EXTENSIONS:
            articles.append(path)

    if not articles and not children:
        return []
    by_name = {path.name.casefold(): path for path in articles}
    overview = next((by_name[name] for name in OVERVIEWS if name in by_name), None)
    sections = []
    if overview:
        sections.append(Section(overview, max(1, level), overview_of=directory))
    elif not root:
        sections.append(Section(directory, level))
    sections.extend(Section(path, level + 1) for path in articles if path != overview)
    sections.extend(children)
    return sections


def _read_document(path: Path, pandoc: str) -> dict:
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Файл должен быть в кодировке UTF-8: {path}") from exc
    proc = subprocess.run(
        [pandoc, "--from=gfm+attributes", "--to=json"],
        input=source, capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode:
        raise ValueError(f"Pandoc не смог прочитать {path}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def _nodes(value: object) -> Iterator[dict]:
    if isinstance(value, list):
        for child in value:
            yield from _nodes(child)
    elif isinstance(value, dict):
        yield value
        if "c" in value:
            yield from _nodes(value["c"])


def _html_indices(value: object) -> None:
    if isinstance(value, dict):
        _html_indices(value.get("c"))
        return
    if not isinstance(value, list):
        return
    pairs = []
    for i, item in enumerate(value):
        _html_indices(item)
        if not isinstance(item, dict) or item.get("t") != "RawInline" or item["c"][0] != "html":
            continue
        tag = HTML_SCRIPT.fullmatch(item["c"][1])
        if tag:
            pairs.append((i, tag[1] == "/", tag[2].lower()))
    if not pairs:
        return
    # Меняем только закрытые пары в той же последовательности строчных элементов.
    stack, ranges = [], {}
    for i, closing, kind in pairs:
        if not closing:
            stack.append((i, kind))
        elif stack and stack[-1][1] == kind:
            start, _ = stack.pop()
            ranges[start] = (i, "Subscript" if kind == "sub" else "Superscript")

    def group(start: int, end: int) -> list:
        out = []
        while start < end:
            if start in ranges:
                stop, tag = ranges[start]
                out.append({"t": tag, "c": group(start + 1, stop)})
                start = stop + 1
            else:
                out.append(value[start])
                start += 1
        return out

    value[:] = group(0, len(value))


def _text(value: str) -> list:
    return [{"t": "Str", "c": value}]


def _header(level: int, title: list, identifier: str = "") -> dict:
    return {"t": "Header", "c": [level, [identifier, [], []], title]}


def _meta_title(document: dict) -> list:
    value = document["meta"].get("title", {})
    if value.get("t") == "MetaInlines":
        return value["c"]
    if value.get("t") == "MetaString":
        return _text(value["c"])
    return []


def _nest_article(blocks: list, level: int, fallback: list) -> None:
    headers = [b for b in blocks if b["t"] == "Header"]
    if not headers:
        blocks.insert(0, _header(level, fallback))
        return
    minimum = min(h["c"][0] for h in headers)
    # Если первая строка озаглавливает всю статью, она становится заголовком её раздела.
    first_is_title = (blocks[0] is headers[0] and headers[0]["c"][0] == minimum
                      and all(h["c"][0] > minimum for h in headers[1:]))
    offset = (level if first_is_title else level + 1) - minimum
    for node in _nodes(blocks):
        if node.get("t") == "Header":
            minimum_level = level if first_is_title and node is headers[0] else level + 1
            node["c"][0] = max(minimum_level, node["c"][0] + offset)
    if not first_is_title:
        blocks.insert(0, _header(level, fallback))


def _register_targets(blocks: list, prefix: str, links: list, path: Path) -> dict[str, str]:
    targets = {"": prefix}
    number = 0
    for node in _nodes(blocks):
        kind, content = node.get("t"), node.get("c")
        if kind == "Link":
            links.append((path, content[2]))
        if kind == "Header":
            attr = content[1]
        elif kind in ATTR_FIRST and content[0][0]:
            attr = content[0]
        else:
            continue
        original = attr[0]
        number += 1
        anchor = prefix if node is blocks[0] else f"{prefix}-{number}"
        if original:
            targets.setdefault(original, anchor)
        attr[0] = anchor
    return targets


def _resolve_links(links: list, targets: dict[Path, dict[str, str]]) -> None:
    for source, target in links:
        try:
            url = urlsplit(target[0])
        except ValueError:
            continue
        if url.scheme or url.netloc or url.query or url.path.startswith("/"):
            continue
        path = Path(os.path.normpath(source.parent / unquote(url.path))) if url.path else source
        anchor = targets.get(path, {}).get(unquote(url.fragment))
        if anchor:
            target[0] = "#" + anchor


def build_book(source: Path, pandoc: str, transform_media: Callable[[dict, Path], None],
               *, exclude_dirs: Iterable[Path] = ()) -> dict:
    """Возвращает Pandoc JSON для одного файла или упорядоченного дерева разделов."""
    source = source.resolve(strict=True)
    single = source.is_file()
    if single and source.suffix.casefold() not in MARKDOWN_EXTENSIONS:
        raise ValueError(f"Ожидается файл .md, .markdown или каталог: {source}")
    base = source.parent if single else source
    plan = ([Section(source, 1)] if single else
            _directory_plan(base, 0, {p.resolve() for p in exclude_dirs}, root=True))
    if not plan:
        raise ValueError(f"В каталоге нет Markdown-документов: {source}")

    combined, targets, links = [], {}, []
    meta = {"title": {"t": "MetaString", "c": source.name or str(source)}}
    version = None
    for section in plan:
        path = section.path
        key = path.relative_to(base).as_posix().encode("utf-8")
        prefix = "sec-" + hashlib.sha256(key).hexdigest()[:24]
        if path.is_dir():
            combined.append(_header(section.level, _text(path.name), prefix))
            targets[path] = {"": prefix}
            continue
        document = _read_document(path, pandoc)
        transform_media(document, path)
        blocks, version = document["blocks"], document["pandoc-api-version"]
        _html_indices(blocks)
        title = _meta_title(document)
        if single:
            meta = {k: v for k, v in document["meta"].items() if k in ("title", "author", "date")}
            first = next((b["c"][2] for b in blocks if b["t"] == "Header"), None)
            meta.setdefault("title", {"t": "MetaInlines", "c": first or _text(path.stem)})
            if not blocks or blocks[0]["t"] != "Header":
                # Якорь файла доступен и для статьи, начинающейся обычным абзацем.
                blocks.insert(0, {"t": "Div", "c": [[prefix, [], []], []]})
        else:
            fallback = section.overview_of.name if section.overview_of else path.stem
            _nest_article(blocks, section.level, title or _text(fallback))
        targets[path] = _register_targets(blocks, prefix, links, path)
        if section.overview_of:
            targets[section.overview_of] = targets[path]
        combined.extend(blocks)
    _resolve_links(links, targets)
    return {"pandoc-api-version": version, "meta": meta, "blocks": combined}
