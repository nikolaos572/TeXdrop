import shutil
import threading
import time
from pathlib import Path

import pytest
from helpers import FAKE_LATEXMK, MINIMAL_TEX, process_exists

from texdrop.compiler import LatexCompiler, tex_directories, tex_live_version
from texdrop.sandbox import landlock_abi


@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "work" / "project").mkdir(parents=True)
    return tmp_path / "work"


def write_project(workdir, files):
    for name, content in files.items():
        path = workdir / "project" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def compile_project(compiler, workdir, main="main.tex", engine="pdflatex", cancelled=None):
    outputs = workdir.parent / "result"
    result = compiler.run(workdir, workdir / "project", main, engine, outputs, cancelled or threading.Event())
    return result, outputs.with_suffix(".log").read_text()


@pytest.mark.parametrize(
    ("output", "version"),
    [("TeX 3.141592653 (TeX Live 2026)\nkpathsea version 6.4.2\n", "TeX Live 2026"), ("something else\n", None)],
)
def test_tex_live_version(tmp_path, monkeypatch, output, version):
    tex = tmp_path / "tex"
    tex.write_text(f"#!/bin/sh\nprintf '{output}'\n")
    tex.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    tex_live_version.cache_clear()
    try:
        assert tex_live_version() == version
        tex.unlink()
        tex_live_version.cache_clear()
        assert tex_live_version() is None  # TeX not installed
    finally:
        tex_live_version.cache_clear()


def test_tex_directories(tmp_path, monkeypatch):
    texlive = (tmp_path / "texlive").resolve()
    (texlive / "texmf-config").mkdir(parents=True)
    kpsewhich = tmp_path / "bin" / "kpsewhich"
    kpsewhich.parent.mkdir()
    kpsewhich.write_text(
        "#!/bin/sh\ncase $1 in\n"
        f"  -var-value=TEXMFROOT) echo {texlive};;\n"  # a TeX Live outside the system directories
        "  -var-value=TEXMFLOCAL) echo /usr/local/share/texmf;;\n"  # readable anyway
        f"  -var-value=TEXMFSYSVAR) echo {Path.home()};;\n"  # never a whole home directory
        f"  -var-value=TEXMFSYSCONFIG) echo {texlive}/texmf-config;;\n"  # inside the first one already
        "  *) echo '{/a,/b}';;\nesac\n"  # not a single directory
    )
    kpsewhich.chmod(0o755)
    monkeypatch.setenv("PATH", str(kpsewhich.parent))
    tex_directories.cache_clear()
    try:
        assert tex_directories() == (str(texlive),)
        kpsewhich.unlink()
        tex_directories.cache_clear()
        assert tex_directories() == ()  # TeX not installed
    finally:
        tex_directories.cache_clear()


@pytest.mark.skipif(not landlock_abi(), reason="Landlock is not available")
def test_tex_directories_are_readable_in_the_sandbox(fake_compiler, workdir, tmp_path, monkeypatch):
    texlive = tmp_path / "texlive"
    texlive.mkdir()
    (texlive / "marker.txt").write_text("TEX-FILE")
    write_project(workdir, {"main.tex": "READTEX", "texdir.txt": str(texlive)})
    monkeypatch.setattr("texdrop.compiler.tex_directories", lambda: ())
    result, log = compile_project(fake_compiler, workdir)
    assert result.error and "Permission denied" in log
    monkeypatch.setattr("texdrop.compiler.tex_directories", lambda: (str(texlive),))
    result, log = compile_project(fake_compiler, workdir)
    assert result.error is None and "TEX-FILE" in log


def test_success(fake_compiler, workdir):
    write_project(workdir, {"main.tex": MINIMAL_TEX})
    result, log = compile_project(fake_compiler, workdir)
    assert result.error is None
    assert result.pdf.read_bytes().startswith(b"%PDF")
    assert result.pdf.parent == workdir.parent  # copied out of the sandbox
    assert "fake-latexmk -norc -pdf -interaction=nonstopmode -file-line-error -f ./main.tex" in log


def test_environment_is_isolated_and_restricted(fake_compiler, workdir):
    write_project(workdir, {"main.tex": MINIMAL_TEX})
    _, log = compile_project(fake_compiler, workdir)
    assert f"HOME={workdir} TMPDIR={workdir}/tmp TEXMFVAR={workdir}/texmf-var" in log
    assert "openout_any=p shell_escape=p" in log


@pytest.mark.parametrize(("engine", "flag"), [("xelatex", "-pdfxe"), ("lualatex", "-pdflua")])
def test_engine_flags(fake_compiler, workdir, engine, flag):
    write_project(workdir, {"main.tex": MINIMAL_TEX})
    _, log = compile_project(fake_compiler, workdir, engine=engine)
    assert f"-norc {flag} " in log


def test_main_file_in_a_subdirectory_runs_there(fake_compiler, workdir):
    write_project(workdir, {"src/thesis.tex": MINIMAL_TEX})
    result, log = compile_project(fake_compiler, workdir, main="src/thesis.tex")
    assert result.error is None and result.pdf is not None
    assert log.splitlines()[0].endswith(" ./thesis.tex")


def test_failure_reports_the_first_errors(fake_compiler, workdir):
    write_project(workdir, {"main.tex": "FAIL"})
    result, _ = compile_project(fake_compiler, workdir)
    assert result.pdf is None
    assert result.error == "./main.tex:3: Undefined control sequence."


def test_a_program_killed_for_memory_is_named(fake_compiler, workdir):
    write_project(workdir, {"main.tex": "OOM"})  # what latexmk reports when the kernel kills lualatex
    result, _ = compile_project(fake_compiler, workdir)
    assert result.error == "lualatex was killed, most likely because the memory ran out."


def test_pdf_is_kept_despite_errors(fake_compiler, workdir):
    write_project(workdir, {"main.tex": "PARTIAL FAIL"})
    result, _ = compile_project(fake_compiler, workdir)
    assert result.pdf is not None and result.error


def test_timeout_kills_the_compilation(workdir):
    compiler = LatexCompiler(1, latexmk=("sh", "-c", FAKE_LATEXMK, "latexmk"), template=workdir / "none")
    write_project(workdir, {"main.tex": "SLEEP"})
    start = time.monotonic()
    result, log = compile_project(compiler, workdir)
    assert time.monotonic() - start < 10
    assert result.error.startswith("Compilation timed out after")
    assert result.error in log


def test_cancel_stops_the_compilation(fake_compiler, workdir):
    write_project(workdir, {"main.tex": "SLEEP"})
    cancelled = threading.Event()
    threading.Timer(0.5, cancelled.set).start()
    start = time.monotonic()
    result, _ = compile_project(fake_compiler, workdir, cancelled=cancelled)
    assert time.monotonic() - start < 10
    assert result.error == "Compilation cancelled."


def test_processes_left_behind_are_killed(fake_compiler, workdir):
    write_project(workdir, {"main.tex": "DAEMON"})
    result, _ = compile_project(fake_compiler, workdir)
    assert result.error is None
    assert not process_exists(int((workdir / "project" / "daemon.pid").read_text()))


def test_cancel_kills_processes_left_behind(fake_compiler, workdir):
    write_project(workdir, {"main.tex": "DAEMON SLEEP"})
    cancelled = threading.Event()
    threading.Timer(1, cancelled.set).start()
    result, _ = compile_project(fake_compiler, workdir, cancelled=cancelled)
    assert result.error == "Compilation cancelled."
    assert not process_exists(int((workdir / "project" / "daemon.pid").read_text()))


def test_memory_limit_reaches_the_compilation(fake_compiler, workdir):
    write_project(workdir, {"main.tex": MINIMAL_TEX})
    assert "limits memory=unlimited" in compile_project(fake_compiler, workdir)[1]
    limited = LatexCompiler(
        30, latexmk=("sh", "-c", FAKE_LATEXMK, "latexmk"), template=workdir / "none", memory_bytes=300 * 1024**2
    )
    assert f"limits memory={300 * 1024}" in compile_project(limited, workdir)[1]  # in KB


def test_disk_limit_stops_the_compilation(workdir):
    compiler = LatexCompiler(
        30, latexmk=("sh", "-c", FAKE_LATEXMK, "latexmk"), template=workdir / "none", disk_bytes=4 * 1024**2
    )
    write_project(workdir, {"main.tex": "DISKFILL"})  # 8 MB in a folder it then makes unreadable
    start = time.monotonic()
    result, log = compile_project(compiler, workdir)
    assert time.monotonic() - start < 10
    assert result.error == "Compilation stopped: its files took more than 4 MB of disk."
    assert result.error in log


def test_planted_symlink_is_not_followed(fake_compiler, workdir, tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text('{"users": {"alice": "token"}}')
    write_project(workdir, {"main.tex": "SYMLINK", "secret-path.txt": str(secret)})
    result, _ = compile_project(fake_compiler, workdir)
    assert (workdir / "project" / "main.pdf").is_symlink()
    assert result.pdf is None


def test_template_is_copied_into_the_work_dir(workdir, tmp_path):
    template = tmp_path / "template"
    (template / "texmf-var").mkdir(parents=True)
    (template / "texmf-var" / "cache").write_text("warm")
    compiler = LatexCompiler(30, latexmk=("sh", "-c", FAKE_LATEXMK, "latexmk"), template=template)
    write_project(workdir, {"main.tex": MINIMAL_TEX})
    compile_project(compiler, workdir)
    assert (workdir / "texmf-var" / "cache").read_text() == "warm"


# --- real TeX (in CI: the TeX Live packages installed by the workflow; in Docker: full TeX Live) ---

latex = pytest.mark.skipif(shutil.which("latexmk") is None, reason="latexmk is not installed")
BIB = "@article{knuth84, author={Donald E. Knuth}, title={Literate Programming}, journal={Comput. J.}, year={1984}}\n"


@pytest.fixture
def real_compiler():
    return LatexCompiler(300)


@pytest.mark.latex
@latex
def test_pdflatex_with_bibtex(real_compiler, workdir):
    write_project(workdir, {
        "main.tex": "\\documentclass{article}\n\\begin{document}\nSee \\cite{knuth84}.\n"
                    "\\bibliographystyle{plain}\n\\bibliography{refs}\n\\end{document}\n",
        "refs.bib": BIB,
    })  # fmt: skip
    result, log = compile_project(real_compiler, workdir)
    assert result.error is None, log
    assert result.pdf.read_bytes().startswith(b"%PDF")
    assert "Running 'bibtex" in log


@pytest.mark.latex
@latex
@pytest.mark.skipif(shutil.which("biber") is None, reason="biber is not installed")
def test_biblatex_with_biber(real_compiler, workdir):
    write_project(workdir, {
        "main.tex": "\\documentclass{article}\n\\usepackage[backend=biber]{biblatex}\n\\addbibresource{refs.bib}\n"
                    "\\begin{document}\nSee \\cite{knuth84}.\n\\printbibliography\n\\end{document}\n",
        "refs.bib": BIB,
    })  # fmt: skip
    result, log = compile_project(real_compiler, workdir)
    assert result.error is None, log
    assert "Running 'biber" in log


@pytest.mark.latex
@latex
@pytest.mark.parametrize("engine", ["xelatex", "lualatex"])
def test_unicode_engines(real_compiler, workdir, engine):
    if shutil.which(engine) is None:
        pytest.skip(f"{engine} is not installed")
    write_project(workdir, {
        "main.tex": "\\documentclass{article}\n\\usepackage{fontspec}\n\\begin{document}\nΚαλημέρα\n\\end{document}\n",
    })  # fmt: skip
    result, log = compile_project(real_compiler, workdir, engine=engine)
    assert result.error is None, log
    assert result.pdf is not None


@pytest.mark.latex
@latex
def test_latex_error_is_reported(real_compiler, workdir):
    write_project(workdir, {"main.tex": "\\documentclass{article}\n\\begin{document}\n\\undefinedmacro\n\\end{document}\n"})
    result, _ = compile_project(real_compiler, workdir)
    assert "./main.tex:3: Undefined control sequence." in result.error


@pytest.mark.latex
@latex
@pytest.mark.skipif(not landlock_abi(), reason="Landlock is not available")
def test_tex_cannot_read_files_outside_the_project(real_compiler, workdir, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-TOKEN")
    write_project(workdir, {
        "main.tex": f"\\documentclass{{article}}\n\\begin{{document}}\n\\input{{{secret}}}\n\\end{{document}}\n",
    })  # fmt: skip
    result, log = compile_project(real_compiler, workdir)
    assert result.error and result.pdf is None
    assert "Permission denied" in log
    assert "TOP-SECRET-TOKEN" not in log
