# TeX Live with the "full" scheme (all ~5000 packages, every engine, biber), from the
# image the TeX Live team publishes (Island of TeX). Overleaf uses the same distribution.
ARG TEXLIVE_IMAGE=texlive/texlive:latest
FROM ${TEXLIVE_IMAGE}

# Like Overleaf: Ghostscript stays (EPS figures, dvipdf), Java goes (the base image installs it for texosquery).
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-venv libarchive13t64 tini \
 && apt-get purge -y --auto-remove $(dpkg-query -W -f '${Package}\n' \
      | grep -E '^(default-jre.*|openjdk-.*|java-common|ca-certificates-java)$') \
 && rm -rf /var/lib/apt/lists/*

# Warm the caches that would otherwise be rebuilt by every job (5-10 s each):
# fontconfig's system cache for XeLaTeX, and a template copied into each job's
# sandbox holding LuaTeX's font database and links to biber's unpacked runtime.
RUN fc-cache -fs > /dev/null \
 && template=/opt/latex-job-template \
 && mkdir -p "$template/biber-cache" /tmp/warmup \
 && PAR_GLOBAL_TEMP=/opt/biber-runtime biber --version > /dev/null \
 && for entry in /opt/biber-runtime/*; do ln -s "$entry" "$template/biber-cache/"; done \
 && rm "$template/biber-cache/inc.lock" && touch "$template/biber-cache/inc.lock" \
 && printf '%s\n' '\documentclass{article}' '\usepackage{fontspec}' \
      '\begin{document}a \textbf{b} \textit{c} \texttt{d} \textsf{e}\end{document}' > /tmp/warmup/a.tex \
 && cd /tmp/warmup \
 && TEXMFCACHE="$template/texmf-var" TEXMFVAR="$template/texmf-var" lualatex -interaction=batchmode a.tex > /dev/null \
 && cd / && rm -rf /tmp/warmup \
 && chmod -R a+rX "$template" /opt/biber-runtime

# The server runs unprivileged as UID/GID 61001: above the range Linux gives to real accounts
# (up to 60000), so it matches no login of the host, such as uid 1000, which often has
# passwordless sudo. A new named volume takes the ownership of /data when it is first
# mounted; a host directory must be given to 61001 (docs/DEPLOYMENT.md).
RUN groupadd --system --gid 61001 app \
 && useradd --system --uid 61001 --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app \
 && mkdir /data && chown app:app /data

WORKDIR /app
# requirements.txt pins every package the image installs, the indirect ones and setuptools
# (which builds TeXdrop) included. --no-deps installs exactly that list, and pip check then
# fails the build if the list is incomplete, instead of pip quietly adding an unpinned package.
COPY requirements.txt ./
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir --no-deps -r requirements.txt
COPY pyproject.toml README.md ./
COPY app ./app
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps --no-build-isolation . && /opt/venv/bin/pip check

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

# Numeric, so that platforms such as Kubernetes can check that it is not root.
USER 61001:61001
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % (os.environ.get('PORT', '').strip() or 8000), timeout=4)"]
# An init process, so that no docker run option is needed for one: it passes docker stop's
# signal on to the server and reaps processes orphaned in the container, which would stay
# as zombies under the server. -s keeps it reaping when Docker's own init runs first (--init).
ENTRYPOINT ["tini", "-s", "--"]
CMD ["python", "-m", "texdrop"]
