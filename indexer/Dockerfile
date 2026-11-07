# RAHBAR indexer image: the only writer of the index. Build context: indexer/, plus two
# named contexts so the corpus tooling and the CI fixtures come from their own folders:
#   docker build -t rahbar-indexer:dev \
#     --build-context corpus=corpus --build-context fixtures=eval/fixtures indexer
FROM python:3.12.16-slim-trixie AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --upgrade pip==25.3 && /opt/venv/bin/pip install -r /tmp/requirements.txt

FROM python:3.12.16-slim-trixie
# tesseract-ocr + poppler-utils: OCR for the scanned PDF (corpus property 3).
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin rahbar \
    && pip uninstall -y pip setuptools wheel || true
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY pipeline ./pipeline
# corpus/fetch.py runs as the Job's init container: it downloads the documents listed in
# sources.yaml into an emptyDir and checks each SHA-256 against the committed manifest.
COPY --from=corpus fetch.py sources.yaml MANIFEST.md ./corpus/
# The three-document fixture corpus the CI e2e job indexes (CORPUS_DIR=/app/fixtures).
COPY --from=fixtures *.md ./fixtures/
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 CORPUS_DIR=/corpus
USER 10001
ENTRYPOINT ["python", "-m", "pipeline.run"]
