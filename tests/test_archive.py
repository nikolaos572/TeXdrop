import gzip
import io
import tarfile
import zipfile

import pytest
from helpers import MINIMAL_TEX

from texdrop.archive import (
    ArchiveError,
    _read_entry,
    detect_format,
    extract_archive,
    inspect_archive,
    is_valid_main,
    resolve_main,
)

LIMIT = 10 * 1024**2
FORMATS = ["zip", "7z", "tar", "tar.gz", "tar.bz2", "tar.xz"]


@pytest.mark.parametrize("fmt", FORMATS)
def test_every_format_is_detected_by_content_and_extracted(make_archive, tmp_path, fmt):
    archive = make_archive({"main.tex": MINIMAL_TEX, "figs/plot.pdf": b"%PDF-1.4"}, fmt)
    renamed = archive.rename(tmp_path / "misleading-name.txt")  # the name is irrelevant

    assert detect_format(renamed).name == fmt
    info = extract_archive(renamed, tmp_path / "out", LIMIT)

    assert info.main == "main.tex"
    assert (tmp_path / "out" / "main.tex").read_text() == MINIMAL_TEX
    assert (tmp_path / "out" / "figs" / "plot.pdf").read_bytes() == b"%PDF-1.4"


@pytest.mark.parametrize("content", [b"", b"just text", b"Rar!\x1a\x07\x00" + b"\0" * 600])
def test_unsupported_files_are_rejected(tmp_path, content):
    path = tmp_path / "upload.zip"
    path.write_bytes(content)
    with pytest.raises(ArchiveError, match="unsupported file type"):
        inspect_archive(path, LIMIT)


def test_corrupted_archive_is_rejected(make_archive, tmp_path):
    data = make_archive({"main.tex": MINIMAL_TEX * 100}, "tar.gz").read_bytes()
    broken = tmp_path / "broken.tar.gz"
    broken.write_bytes(data[: len(data) // 2])
    with pytest.raises(ArchiveError, match="cannot read the tar.gz archive"):
        inspect_archive(broken, LIMIT)


def test_gzip_that_is_not_a_tar_is_rejected(tmp_path):
    path = tmp_path / "main.tex.gz"
    path.write_bytes(gzip.compress(MINIMAL_TEX.encode()))
    with pytest.raises(ArchiveError):
        inspect_archive(path, LIMIT)


@pytest.mark.parametrize("name", ["../evil.tex", "a/../../evil.tex", "/etc/evil.tex", "a/\x01b.tex"])
def test_unsafe_paths_are_rejected(make_archive, tmp_path, name):
    archive = make_archive({"main.tex": MINIMAL_TEX, name: "x"}, "tar")
    with pytest.raises(ArchiveError, match="unsafe path"):
        extract_archive(archive, tmp_path / "out", LIMIT)
    assert not (tmp_path / "evil.tex").exists()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_links_are_rejected(tmp_path, kind):
    path = tmp_path / "links.tar"
    with tarfile.open(path, "w") as archive:
        archive.addfile(_member("main.tex", MINIMAL_TEX), io.BytesIO(MINIMAL_TEX.encode()))
        link = tarfile.TarInfo("secret.tex")
        link.type = kind
        link.linkname = "/data/config.json" if kind == tarfile.SYMTYPE else "main.tex"
        archive.addfile(link)
    with pytest.raises(ArchiveError, match="links are not allowed"):
        inspect_archive(path, LIMIT)


def test_special_files_are_rejected(tmp_path):
    path = tmp_path / "fifo.tar"
    with tarfile.open(path, "w") as archive:
        fifo = tarfile.TarInfo("pipe")
        fifo.type = tarfile.FIFOTYPE
        archive.addfile(fifo)
    with pytest.raises(ArchiveError, match="special files"):
        inspect_archive(path, LIMIT)


def test_zip_symlink_is_rejected(tmp_path):
    path = tmp_path / "link.zip"
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("link.tex")
        info.external_attr = (0o120777 << 16)  # symlink mode bits
        archive.writestr(info, "/data/config.json")
    with pytest.raises(ArchiveError, match="links are not allowed"):
        inspect_archive(path, LIMIT)


def test_size_limit_uses_declared_sizes(make_archive):
    archive = make_archive({"main.tex": MINIMAL_TEX, "big.bin": b"\0" * 5000})
    with pytest.raises(ArchiveError, match="unpacks to more than"):
        inspect_archive(archive, 4000)


class _UnderstatedEntry:
    """An entry whose header claims 1 byte but which holds 200."""

    size = 1

    def get_blocks(self):
        yield b"x" * 100
        yield b"y" * 100


def test_size_limit_counts_actual_bytes_when_extracting(tmp_path):
    assert _read_entry(_UnderstatedEntry(), "a.bin", tmp_path, total=1, max_bytes=500) == (False, 200)
    with pytest.raises(ArchiveError, match="unpacks to more than"):
        _read_entry(_UnderstatedEntry(), "b.bin", tmp_path, total=1, max_bytes=150)


def test_too_many_entries(make_archive, monkeypatch):
    monkeypatch.setattr("texdrop.archive.MAX_ENTRIES", 3)
    archive = make_archive({f"f{i}.tex": "x" for i in range(4)})
    with pytest.raises(ArchiveError, match="more than 3 entries"):
        inspect_archive(archive, LIMIT)


def test_empty_archive_is_rejected(make_archive):
    with pytest.raises(ArchiveError, match="contains no files"):
        inspect_archive(make_archive({}), LIMIT)


def test_single_top_level_folder_is_the_project_root(make_archive, tmp_path):
    archive = make_archive({"paper/main.tex": MINIMAL_TEX, "paper/sec/intro.tex": "Intro", "paper/refs.bib": ""})
    info = extract_archive(archive, tmp_path / "out", LIMIT)
    assert info.root == "paper/"
    assert info.tex_files == ("main.tex", "sec/intro.tex")
    assert info.main == "main.tex"
    assert (tmp_path / "out" / "paper" / "main.tex").is_file()


def test_nested_single_folders_are_unwrapped(make_archive):
    info = inspect_archive(make_archive({"a/b/main.tex": MINIMAL_TEX, "a/b/x.bib": ""}), LIMIT)
    assert (info.root, info.main) == ("a/b/", "main.tex")


def test_macos_metadata_is_ignored(make_archive):
    archive = make_archive(
        {"paper/main.tex": MINIMAL_TEX, "__MACOSX/paper/._main.tex": "junk", "paper/.DS_Store": "junk", "paper/._x.tex": "junk"}
    )
    info = inspect_archive(archive, LIMIT)
    assert (info.root, info.tex_files, info.main) == ("paper/", ("main.tex",), "main.tex")


def test_only_top_level_tex_file_is_detected(make_archive):
    info = inspect_archive(make_archive({"thesis.tex": "no class here", "chapters/one.tex": "\\documentclass{x}"}), LIMIT)
    assert info.main == "thesis.tex"


def test_documentclass_picks_the_main_among_several(make_archive):
    files = {
        "macros.tex": "\\newcommand{\\x}{y}",
        "paper.tex": "% comment\n\\documentclass[11pt]{article}\n\\input{macros}",
        "notes.tex": "% \\documentclass{article} is commented out",
    }
    info = inspect_archive(make_archive(files), LIMIT)
    assert info.tex_files == ("macros.tex", "notes.tex", "paper.tex")
    assert info.main == "paper.tex"


def test_several_documents_are_ambiguous(make_archive):
    info = inspect_archive(make_archive({"a.tex": MINIMAL_TEX, "b.tex": MINIMAL_TEX}), LIMIT)
    assert info.main is None
    with pytest.raises(ArchiveError, match="several .tex files found.*a.tex, b.tex"):
        resolve_main(info, None)
    assert resolve_main(info, "b.tex") == "b.tex"


def test_no_top_level_tex_file(make_archive):
    info = inspect_archive(make_archive({"src/main.tex": MINIMAL_TEX, "README.md": "x"}), LIMIT)
    assert (info.main, info.tex_files) == (None, ("src/main.tex",))
    assert resolve_main(info, "src/main.tex") == "src/main.tex"


def test_no_tex_file_at_all(make_archive):
    info = inspect_archive(make_archive({"README.md": "x"}), LIMIT)
    with pytest.raises(ArchiveError, match="no .tex file"):
        resolve_main(info, None)
    with pytest.raises(ArchiveError, match="no usable .tex file"):
        resolve_main(info, "main.tex")


@pytest.mark.parametrize("requested", ["other.tex", "../main.tex", "/main.tex", "main.tex\n", "MAIN.TEX", "main"])
def test_requested_main_must_be_a_listed_tex_file(make_archive, requested):
    info = inspect_archive(make_archive({"main.tex": MINIMAL_TEX}), LIMIT)
    if requested.strip() == "main.tex":
        assert resolve_main(info, requested) == "main.tex"  # surrounding whitespace is ignored
    else:
        with pytest.raises(ArchiveError, match="is not a .tex file in the archive"):
            resolve_main(info, requested)


def test_requested_main_may_include_the_root_folder(make_archive):
    info = inspect_archive(make_archive({"paper/main.tex": MINIMAL_TEX, "paper/b.tex": MINIMAL_TEX}), LIMIT)
    assert resolve_main(info, "paper/b.tex") == "b.tex"


@pytest.mark.parametrize(
    ("path", "valid"),
    [
        ("main.tex", True),
        ("My Paper (v2).tex", True),
        ("κύριο.tex", True),
        ("src/main.tex", True),
        ("main.TEX", True),
        ("main.pdf", False),
        ("-pdflua.tex", False),
        ("a$(id).tex", False),
        ('a".tex', False),
        ("a`id`.tex", False),
        ("a;b.tex", False),
        ("dir/-x/main.tex", False),
    ],
)
def test_main_file_names_are_shell_safe(path, valid):
    assert is_valid_main(path) is valid


def test_unsafe_tex_names_are_not_offered(make_archive):
    info = inspect_archive(make_archive({"main.tex": MINIMAL_TEX, "a$(reboot).tex": MINIMAL_TEX}), LIMIT)
    assert info.tex_files == ("main.tex",)
    with pytest.raises(ArchiveError):
        resolve_main(info, "a$(reboot).tex")


def test_file_and_directory_with_the_same_name(make_archive, tmp_path):
    archive = make_archive({"main.tex": MINIMAL_TEX, "x": "file", "x/y.tex": "nested"}, "tar")
    with pytest.raises(ArchiveError, match="cannot extract 'x/y.tex'"):
        extract_archive(archive, tmp_path / "out", LIMIT)


def _member(name: str, content: str) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.size = len(content.encode())
    return member
