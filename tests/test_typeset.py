import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import pipeline


CACHE_FAILURE = (
    'luaotfload | load : FATAL ERROR\n'
    'luaotfload | load : Failed to load "fontloader" module "basics-gen".\n'
    'luaotfload-init.lua:301: system : no writeable ca\nche path, quiting'
)


class TypesetTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.calls = []
        self.sleep = self.enter_patch(patch("pipeline.time.sleep"))

    def enter_patch(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def typeset(self, *outcomes):
        events = iter(outcomes)

        def run(command, *, cwd, data=None):
            if command[0] == "pandoc":
                return
            self.calls.append(command)
            outcome = next(events)
            if isinstance(outcome, ValueError):
                if str(outcome):
                    (cwd / "book.log").write_text(str(outcome), encoding="utf-8")
                raise outcome
            (cwd / "book.aux").write_text(outcome, encoding="utf-8")
            (cwd / "book.pdf").write_bytes(b"%PDF-")

        with patch("pipeline.run", side_effect=run):
            pipeline.typeset({}, self.work, "pandoc", "lualatex", True, True)

    def test_retries_same_pass_until_it_produces_stable_references(self):
        error = ValueError(CACHE_FAILURE)
        self.typeset("A", error, error, "B", "B")
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])
        self.assertEqual(self.calls[1:], [self.calls[0]] * 4)
        self.assertIn("--disable-installer", self.calls[0])

    def test_exhausts_two_retries_even_if_first_pass_left_pdf(self):
        error = ValueError(CACHE_FAILURE)
        with self.assertRaises(ValueError) as raised:
            self.typeset("A", error, error, error)
        self.assertIs(raised.exception, error)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual((self.work / "book.log").read_text(encoding="utf-8"), CACHE_FAILURE)

    def test_other_latex_or_font_failures_stop_immediately(self):
        for message in ("! Undefined control sequence.",
                        "luaotfload | load : FATAL ERROR\nCannot load a font.",
                        "system : no writeable cache path"):
            with self.subTest(message=message):
                self.calls.clear()
                error = ValueError(message)
                with self.assertRaises(ValueError) as raised:
                    self.typeset(error)
                self.assertIs(raised.exception, error)
                self.assertEqual(len(self.calls), 1)
                self.sleep.assert_not_called()

    def test_failure_without_new_log_uses_no_stale_cache_diagnostic(self):
        second_error = ValueError()
        with self.assertRaises(ValueError) as raised:
            self.typeset(ValueError(CACHE_FAILURE), second_error)
        self.assertIs(raised.exception, second_error)
        self.assertEqual(len(self.calls), 2)
        self.sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
