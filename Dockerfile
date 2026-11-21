# Blast Radius selector for sqlglot. Built and scored on linux/amd64.
# The base digest is the image every commit was labeled in: its Python and
# SQLite decide which sqlglot tests run, and we run some of them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

# sqlglot at the pinned commit, from its own repository.
RUN python -c "import io,tarfile,urllib.request;d=urllib.request.urlopen('https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa',timeout=300).read();tarfile.open(fileobj=io.BytesIO(d)).extractall('/opt',filter='data')" \
 && mv /opt/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /opt/sqlglot

COPY br /app/br
COPY maps /app/maps
RUN python -m compileall -q -j 4 /opt/sqlglot /app/br /usr/local/lib/python3.13 > /dev/null 2>&1; \
    (cd /app && SKIP_INTEGRATION=0 python -m br.buildstate /app/state.marshal /opt/sqlglot /app/maps 2>/dev/null \
     || echo "state snapshot failed: clean mode disabled"); \
    chmod -R a+rX /opt/sqlglot /app

ENV SKIP_INTEGRATION=0 BR_ROOT=/opt/sqlglot BR_MAPS=/app/maps BR_VERIFY=1 BR_CPU_FACTOR=1.5
WORKDIR /app
CMD ["python", "-m", "br"]
