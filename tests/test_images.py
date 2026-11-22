import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from images import ImageCache


def picture(color, fmt="PNG", **options):
    data = BytesIO()
    with Image.new("RGB", (8, 8), color) as image:
        image.save(data, format=fmt, **options)
    return data.getvalue()


def parse(text):
    run = subprocess.run(["pandoc", "--from=gfm+attributes", "--to=json"], input=text,
                         capture_output=True, encoding="utf-8", check=True)
    return json.loads(run.stdout)


def image_paths(node):
    if isinstance(node, list):
        for item in node:
            yield from image_paths(item)
    elif isinstance(node, dict):
        if node.get("t") == "Image":
            yield node["c"][2][0]
        else:
            for item in node.values():
                yield from image_paths(item)


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mdpdf-images-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cache = self.root / "общий кеш % #"
        self.work = self.root / "сборка"
        self.media = ImageCache(self.cache, work_dir=self.work)

    def prepare(self, markdown, source, cache=None):
        document = parse(markdown)
        (cache or self.media).transform_media(document, source)
        return list(image_paths(document))

    def test_relative_paths_content_changes_and_literal_code(self):
        for directory, color in (("первая", "red"), ("вторая", "blue")):
            folder = self.root / directory
            folder.mkdir()
            (folder / "схема #1.png").write_bytes(picture(color))
        first, second = (self.root / name / "статья.md" for name in ("первая", "вторая"))
        text = '![Фото](<схема %231.png>)\n\n`![пример](пропущен.png)`'
        first_path, = self.prepare(text, first)
        second_path, = self.prepare(text, second)
        self.assertNotEqual(first_path, second_path)
        self.assertTrue(first_path.isascii())
        (first.parent / "схема #1.png").write_bytes(picture("blue"))
        self.assertEqual(self.prepare(text, first), [second_path])

    def test_remote_html_image_survives_cache_transfer_and_offline_mode(self):
        jpg = picture("green", "JPEG")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(jpg)))
                self.end_headers()
                self.wfile.write(jpg)

            def log_message(self, *args):
                pass

        source = self.root / "статья.md"
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/figure.png?v=2"
                markdown = f'<p>Рисунок <img src="{url}" alt="Фото" width="50">.</p>'
                target, = self.prepare(markdown, source)
            finally:
                server.shutdown()
                worker.join()
        self.assertEqual((self.work / target).read_bytes(), jpg)
        other = self.root / "переданный кеш"
        shutil.copytree(self.cache, other)
        work = self.root / "другая сборка"
        offline = ImageCache(other, True, work_dir=work)
        with patch("images.urlopen", side_effect=AssertionError("Сеть недоступна")):
            self.assertEqual(self.prepare(markdown, source, offline), [target])
            with self.assertRaisesRegex(RuntimeError, "отсутствует в кеше"):
                self.prepare(f"![]({url}1)", source, offline)
        self.assertEqual((work / target).read_bytes(), jpg)

    def test_svg_and_first_frame_by_content(self):
        source = self.root / "глава.md"
        (self.root / "shape.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
            '<rect width="8" height="8" fill="red"/></svg>', encoding="utf-8")
        with Image.new("RGB", (8, 8), "blue") as second:
            (self.root / "animation.png").write_bytes(picture(
                "red", "WEBP", save_all=True, append_images=[second], duration=20, lossless=True))
        paths = self.prepare("![](shape.svg) ![](animation.png)", source)
        self.assertEqual(len(paths), 2)
        for path in paths:
            with Image.open(self.work / path) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.convert("RGB").getpixel((3, 3)), (255, 0, 0))

    def test_parallel_builds_share_complete_assets(self):
        shared = self.root / "рисунок.png"
        payload = picture("green")
        shared.write_bytes(payload)

        def build(index):
            work = self.root / str(index)
            media = ImageCache(self.cache, work_dir=work)
            node = {"t": "Image", "c": [["", [], []], [], [shared.name, ""]]}
            media.transform_media(node, self.root / "глава.md")
            self.assertEqual((work / node["c"][2][0]).read_bytes(), payload)
            return node["c"][2][0]

        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(len(set(pool.map(build, range(24)))), 1)
        self.assertEqual(list(self.cache.rglob(".part-*")), [])

    def test_mermaid_retries_nested_labels_only_after_renderer_rejects_original(self):
        code = 'flowchart LR\nA[dp[i]] --> B["f(x)"]\nB --> C[(База)]'
        escaped = code.replace("A[dp[i]]", 'A["dp[i]"]')
        received = []

        def render(command, directory, text):
            received.append(text)
            if text == code:
                return subprocess.CompletedProcess(command, 1, stderr=b"Parse error")
            (directory / "diagram.png").write_bytes(picture("red"))
            return subprocess.CompletedProcess(command, 0)

        def block(text):
            return {"t": "CodeBlock", "c": [["", ["mermaid"], []], text]}

        with patch("images._mermaid_command", return_value=["mmdc"]), patch("images._run_mermaid", render):
            node = block(code)
            self.media.transform_media(node, self.root / "глава.md")
            self.assertEqual(received, [code, escaped])
            self.assertTrue((self.work / next(image_paths(node))).is_file())
            self.media.transform_media(block(escaped), self.root / "глава.md")
            self.assertEqual(received, [code, escaped, escaped])


if __name__ == "__main__":
    unittest.main()
