from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from http.client import HTTPException
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, unquote_to_bytes, urldefrag, urlsplit
from urllib.request import Request, urlopen


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _publish(path: Path, data: bytes, *, replace: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".part-", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_bytes(data)
        if replace or os.name != "nt":
            os.replace(temporary, path)
        else:
            # В Windows другой процесс уже может читать опубликованный файл.
            try:
                temporary.rename(path)
            except (FileExistsError, PermissionError):
                if not path.is_file():
                    raise
    finally:
        temporary.unlink(missing_ok=True)


def _download(url: str) -> bytes:
    parts = urlsplit(url)
    legacy = re.fullmatch(r"/problems/[^/]+/Figures/(\d+/.+)", parts.path)
    if parts.netloc.lower() in {"leetcode.com", "www.leetcode.com"} and legacy:
        parts = parts._replace(scheme="https", netloc="assets.leetcode.com",
                               path="/static_assets/media/original_images/" + legacy[1])
    url = parts._replace(path=quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
                         query=quote(parts.query, safe="/?%:@!$&'()*+,;=-._~")).geturl()
    request = Request(url, headers={"User-Agent": "md-pdf-pipeline/1.0"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=10) as response:
                return response.read()
        except HTTPError as exc:
            exc.close()
            if exc.code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                raise
        except (URLError, OSError, HTTPException):
            if attempt == 2:
                raise
    raise RuntimeError("Исчерпаны попытки загрузки изображения")


def _convert(data: bytes) -> tuple[str, bytes]:
    if data.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"<"):
        import resvg_py

        return ".png", resvg_py.svg_to_bytes(svg_string=data.decode("utf-8-sig"))
    from PIL import Image

    with Image.open(BytesIO(data)) as image:
        image.load()
        if image.format == "JPEG":
            return ".jpg", data
        if image.format == "PNG" and not image.is_animated:
            return ".png", data
        output = BytesIO()
        image.convert("RGBA").save(output, format="PNG")
        return ".png", output.getvalue()


def _mermaid_command() -> list[str]:
    if os.name == "nt":
        shim = shutil.which("mmdc.cmd")
        node = shutil.which("node.exe")
        if shim and node:
            directory = Path(shim).parent
            # npm создаёт разные обёртки при локальной и глобальной установке.
            for modules in (directory / "node_modules", directory.parent):
                package = modules / "@mermaid-js" / "mermaid-cli"
                manifest = package / "package.json"
                if manifest.is_file():
                    entry = json.loads(manifest.read_text(encoding="utf-8"))["bin"]
                    script = package / (entry["mmdc"] if isinstance(entry, dict) else entry)
                    if script.is_file():
                        return [node, str(script)]
    else:
        program = shutil.which("mmdc")
        if program:
            return [program]
    raise RuntimeError("Для построения схем Mermaid установите Node.js и @mermaid-js/mermaid-cli (INSTALL-GUIDE.md)")


def _quote_mermaid_labels(code: str) -> str:
    if not re.search(r"(?m)^\s*(?:flowchart|graph)\s", code):
        return code
    tokens = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|(?<=[\w.-])\[(?!["'\[(/\\])''')

    def rewrite(line: str) -> str:
        pieces, cursor = [], 0
        for match in tokens.finditer(line):
            if match[0] != "[" or match.start() < cursor:
                continue
            depth = 1
            for end in range(match.end(), len(line)):
                if line[end] == "[":
                    depth += 1
                elif line[end] == "]":
                    depth -= 1
                    if depth == 0:
                        break
            if depth:
                continue
            label = line[match.end():end]
            if not any(ch in label for ch in "[]()"):
                continue
            pieces.append(line[cursor:match.start()])
            pieces.append('["' + label.replace("\\", "\\\\").replace('"', '\\"') + '"]')
            cursor = end + 1
        return "".join(pieces) + line[cursor:]

    return "".join(rewrite(line) for line in code.splitlines(keepends=True))


def _run_mermaid(command: list[str], directory: Path, code: str) -> subprocess.CompletedProcess:
    (directory / "diagram.mmd").write_text(code, encoding="utf-8")
    return subprocess.run([*command, "-i", "diagram.mmd", "-o", "diagram.png", "--quiet"],
                          cwd=directory, capture_output=True, timeout=90)


class _ImageTag(HTMLParser):
    """Одиночный HTML-тег изображения, включая размер и подпись."""

    def __init__(self):
        super().__init__()
        self.node = None
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += 1
        attrs = dict(attrs)
        if tag != "img" or not attrs.get("src"):
            return
        alt = [{"t": "Space"} if token.isspace() else {"t": "Str", "c": token}
               for token in re.findall(r"\S+|\s+", attrs.get("alt") or "")]
        attr = [attrs.get("id") or "", (attrs.get("class") or "").split(),
                [[key, attrs[key]] for key in ("width", "height") if attrs.get(key)]]
        self.node = {"t": "Image", "c": [attr, alt, [attrs["src"], attrs.get("title") or ""]]}

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if data.strip():
            self.count += 1

    def handle_endtag(self, tag):
        if tag != "img":
            self.count += 1


class ImageCache:
    def __init__(self, cache_dir: Path, offline: bool = False, *, work_dir: Path, pandoc: str = "pandoc"):
        self.directory = Path(cache_dir)
        self.assets = Path(work_dir) / "assets"
        self.offline = offline
        self.pandoc = pandoc

    def _image(self, data: bytes) -> Path:
        stem = self.directory / "images" / _digest(data)
        for extension in (".png", ".jpg"):
            target = stem.with_suffix(extension)
            if target.is_file():
                return target
        extension, converted = _convert(data)
        target = stem.with_suffix(extension)
        _publish(target, converted)
        return target

    def _remote(self, src: str) -> Path:
        url = urldefrag(src).url
        index = self.directory / "urls" / (_digest(url.encode("utf-8")) + ".txt")
        existing = index.is_file()
        if existing:
            name = index.read_text(encoding="utf-8")
            if re.fullmatch(r"[0-9a-f]{64}\.(?:png|jpg)", name):
                target = self.directory / "images" / name
                if target.is_file():
                    return target
        if self.offline:
            raise ValueError("изображение отсутствует в кеше, включён автономный режим")
        target = self._image(_download(url))
        _publish(index, target.name.encode("utf-8"), replace=existing)
        return target

    def _stage(self, target: Path) -> str:
        self.assets.mkdir(parents=True, exist_ok=True)
        staged = self.assets / target.name
        if not staged.exists():
            try:
                os.link(target, staged)
            except OSError:
                shutil.copyfile(target, staged)
        return "assets/" + target.name

    def _resolve(self, src: str, source: Path) -> str:
        label = src
        try:
            parts = urlsplit(src)
            if parts.scheme in ("http", "https"):
                label = (parts.hostname or "") + parts.path
                target = self._remote(src)
            elif parts.scheme == "data":
                label = "встроенное изображение"
                header, payload = src.split(",", 1)
                if not header.lower().startswith("data:image/"):
                    raise ValueError("ожидается data:image/")
                data = unquote_to_bytes(payload)
                if header.lower().endswith(";base64"):
                    data = base64.b64decode(data, validate=True)
                target = self._image(data)
            elif not parts.scheme or (os.name == "nt" and re.match(r"^[a-zA-Z]:[/\\]", src)):
                path = Path(unquote(parts.path if not parts.scheme else src))
                target = self._image((source.parent / path).read_bytes())
            else:
                raise ValueError("неподдерживаемая схема адреса " + parts.scheme)
            return self._stage(target)
        except Exception as exc:
            raise RuntimeError(f"{source}: не удалось подготовить изображение «{label}»: {exc}") from exc

    def _mermaid(self, code: str, source: Path) -> str:
        key = _digest(code.encode("utf-8"))
        target = self.directory / "mermaid" / f"mermaid-{key}.png"
        try:
            if not target.is_file():
                command = _mermaid_command()
                with tempfile.TemporaryDirectory(prefix="mdpdf-mermaid-") as tmp:
                    directory = Path(tmp)
                    run = _run_mermaid(command, directory, code)
                    if run.returncode:
                        quoted = _quote_mermaid_labels(code)
                        if quoted != code:
                            run = _run_mermaid(command, directory, quoted)
                    if run.returncode:
                        raise ValueError((run.stderr or run.stdout).decode("utf-8", errors="replace").strip())
                    _publish(target, (directory / "diagram.png").read_bytes())
            return self._stage(target)
        except Exception as exc:
            raise RuntimeError(f"{source}: не удалось построить схему Mermaid: {exc}") from exc

    def _html(self, fragment: str, kind: str) -> dict:
        tag = _ImageTag()
        tag.feed(fragment)
        tag.close()
        if tag.count == 1 and tag.node:
            if kind == "RawInline":
                return tag.node
            return {"t": "Para", "c": [tag.node]}
        result = subprocess.run([self.pandoc, "--from=html", "--to=json"], input=fragment,
                                encoding="utf-8", capture_output=True, check=True)
        blocks = json.loads(result.stdout)["blocks"]
        if kind == "RawInline":
            inlines = [item for block in blocks if block["t"] in {"Plain", "Para"} for item in block["c"]]
            return {"t": "Span", "c": [["", [], []], inlines]}
        return {"t": "Div", "c": [["", [], []], blocks]}

    def transform_media(self, document: dict, source_file: Path) -> None:
        def visit(node):
            if isinstance(node, list):
                for child in node:
                    visit(child)
                return
            if not isinstance(node, dict):
                return
            kind, content = node.get("t"), node.get("c")
            if kind in {"RawInline", "RawBlock"} and content[0] == "html" and re.search(r"<img\b", content[1], re.I):
                node.update(self._html(content[1], kind))
                visit(node)
            elif kind == "Image":
                content[2][0] = self._resolve(content[2][0], source_file)
            elif kind == "CodeBlock" and "mermaid" in [name.lower() for name in content[0][1]]:
                image = self._mermaid(content[1], source_file)
                node.update(t="Para", c=[{"t": "Image", "c": [[content[0][0], [], []], [], [image, ""]]}])
            else:
                for child in node.values():
                    visit(child)

        visit(document)
