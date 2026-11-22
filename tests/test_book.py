import shutil
import tempfile
import unittest
from pathlib import Path

from book import build_book


@unittest.skipUnless(shutil.which("pandoc"), "Требуется Pandoc")
class BookTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def write(self, relative, content):
        file = self.base / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
        return file

    def build(self, source, **kwargs):
        return build_book(source, "pandoc", lambda doc, file: None, **kwargs)

    def test_tree_scopes_anchors_and_uses_index_as_directory_heading(self):
        self.write("Курс/README.md", "# Раздел\n\n[Тема](2%20факта.MD#пример)\n")
        self.write("Курс/2 факта.MD", "# Два\n\n## Пример\n\n[Ещё](10.md#пример)\n")
        self.write("Курс/10.md", "# Десять\n\n## Пример\n\n[К разделу](./)\n")
        self.write("Курс/12/далее.markdown", "Обычный абзац.\n")
        self.write(".служебная/text.md", "Скрытая статья")
        self.write("Кэш/text.md", "Служебная статья")
        doc = self.build(self.base, exclude_dirs=[self.base / "Кэш"])
        headings = [b["c"] for b in doc["blocks"] if b["t"] == "Header"]
        self.assertEqual([(level, title[0]["c"]) for level, _, title in headings],
                         [(1, "Раздел"), (2, "Два"), (3, "Пример"), (2, "Десять"),
                          (3, "Пример"), (2, "12"), (3, "далее")])
        ids = [attrs[0] for _, attrs, _ in headings]
        self.assertEqual(len(set(ids)), len(ids))
        refs = [b["c"][0]["c"][2][0] for b in doc["blocks"]
                if b["t"] == "Para" and b["c"][0]["t"] == "Link"]
        self.assertEqual(refs, ["#" + ids[2], "#" + ids[4], "#" + ids[0]])

    def test_single_file_preserves_metadata_math_and_code(self):
        path = self.write("один.md", "---\ntitle: Название\nauthor: Автор\ndate: 2025\n---\n\n"
                          "Текст $a<b$, x<sup>2</sup> и `$не_формула$`.\n\n"
                          "~~~math\nx^2\n~~~\n\n~~~text\n# пример\n~~~\n")
        doc = self.build(path)
        self.assertEqual(set(doc["meta"]), {"title", "author", "date"})
        nodes = [n for b in doc["blocks"] if b["t"] == "Para" for n in b["c"]]
        self.assertEqual([n["c"] for n in nodes if n["t"] == "Math"],
                         [[{"t": "InlineMath"}, "a<b"], [{"t": "DisplayMath"}, "x^2"]])
        self.assertEqual([n["c"][0]["c"] for n in nodes if n["t"] == "Superscript"], ["2"])
        self.assertEqual([n["c"][1] for n in nodes if n["t"] == "Code"], ["$не_формула$"])
        self.assertEqual(doc["blocks"][-1]["c"][1], "# пример")

    def test_multiple_top_headings_keep_their_relative_levels(self):
        self.write("Разбор.md", "# Раз\n\n## Пример {#detail}\n\n# Два\n\n"
                   "[К примеру](#detail)\n")
        doc = self.build(self.base)
        headers = [b["c"] for b in doc["blocks"] if b["t"] == "Header"]
        self.assertEqual([(h[0], h[2][0]["c"]) for h in headers],
                         [(1, "Разбор"), (2, "Раз"), (3, "Пример"), (2, "Два")])
        self.assertEqual(doc["blocks"][-1]["c"][0]["c"][2][0], "#" + headers[2][1][0])

    def test_empty_tree_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "нет Markdown-документов"):
            self.build(self.base)


if __name__ == "__main__":
    unittest.main()
