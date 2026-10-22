# TeX Live with the "full" scheme (all ~5000 packages, every engine, biber), from the
# image the TeX Live team publishes (Island of TeX). Overleaf uses the same distribution.
ARG TEXLIVE_IMAGE=texlive/texlive:latest
FROM ${TEXLIVE_IMAGE}

ARG APP_UID=1001

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

RUN useradd --uid "$APP_UID" --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app \
 && mkdir /data && chown app:app /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir .

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

USER app
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % (os.environ.get('PORT', '').strip() or 8000), timeout=4)"]
# An init process, so that no docker run option is needed for one: it passes docker stop's
# signal on to the server and reaps processes orphaned in the container, which would stay
# as zombies under the server. -s keeps it reaping when Docker's own init runs first (--init).
ENTRYPOINT ["tini", "-s", "--"]
CMD ["python", "-m", "texdrop"]
