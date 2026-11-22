import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from tools.prepare_miktex import DATABASES, prepare


def database(path, name, content):
    with tarfile.open(path, "w:xz") as archive:
        entry = tarfile.TarInfo(name)
        entry.size = len(content)
        archive.addfile(entry, io.BytesIO(content))


class PrepareMiKTeXTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        base = Path(root.name)
        self.src = base / "Полный репозиторий"
        self.src.mkdir()
        self.dest = base / "Локальные пакеты"
        self.ids = base / "packages.txt"
        self.ids.write_text("# Пример\ncollection\n", encoding="utf-8")
        (self.src / "pr.ini").write_text("[repository]\nversion=1\n", encoding="utf-8")
        archives, manifests = [], ["[collection]\nrequire[]=main\n"]
        for name, kind, extension, dependencies in [
            ("main", "MSCab", ".cab", "require[]=nested\n"),
            ("nested", "TarBzip2", ".tar.bz2", ""),
        ]:
            content = (name * 3).encode("utf-8")
            checksum = hashlib.md5(content).hexdigest()
            (self.src / (name + extension)).write_bytes(content)
            archives.append(f"[{name}]\nType={kind}\nCabSize={len(content)}\n"
                            f"CabMD5={checksum}\nMD5={checksum}\n")
            manifests.append(f"[{name}]\ndigest={checksum}\n{dependencies}")
        database(self.src / DATABASES[0], "mpm.ini", "".join(archives).encode("utf-8"))
        database(self.src / DATABASES[1], "texmf/tpm/packages/main.tpm", b"<tpm/>")
        database(self.src / DATABASES[2], "package-manifests.ini", "".join(manifests).encode("utf-8"))

    def build(self):
        prepare(str(self.src), self.dest, self.ids)

    def test_transitive_types_reuse_and_snapshot_change(self):
        self.build()
        self.assertEqual((self.dest / "nested.tar.bz2").read_bytes(), b"nestednestednested")
        self.assertEqual((self.dest / "main.cab").read_bytes(), b"mainmainmain")
        (self.src / "main.cab").unlink()
        self.build()
        previous_metadata = (self.dest / "pr.ini").read_bytes()
        (self.src / "pr.ini").write_bytes(b"[repository]\nversion=2\n")
        with self.assertRaisesRegex(ValueError, "Снимок"):
            self.build()
        self.assertEqual((self.dest / "pr.ini").read_bytes(), previous_metadata)

    def test_corrupt_package_is_not_published(self):
        (self.src / "nested.tar.bz2").write_bytes(b"wrong")
        with self.assertRaisesRegex(ValueError, "контрольная сумма"):
            self.build()
        self.assertFalse((self.dest / "pr.ini").exists())
        self.assertFalse((self.dest / "nested.tar.bz2").exists())
        (self.src / "nested.tar.bz2").write_bytes(b"nestednestednested")
        self.build()
        self.assertTrue((self.dest / "pr.ini").exists())


if __name__ == "__main__":
    unittest.main()
