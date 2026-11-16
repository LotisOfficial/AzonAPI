# AZON API image: Python 3.12 + Java 8 (Temurin), non-root, no SSH, no debug server.
# NOT YET BUILT OR TESTED - see docs/BUILD_LOG.md.
# Pin both base images by digest before production use.

FROM eclipse-temurin:8-jre-jammy AS java

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JAVA_HOME=/opt/java/openjdk \
    PATH=/opt/java/openjdk/bin:$PATH

COPY --from=java /opt/java/openjdk /opt/java/openjdk

RUN useradd --uid 10001 --create-home azon

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY essential_open_api/ essential_open_api/
COPY jars/ jars/
COPY resources/forms/ resources/forms/
COPY wsgi.py .

USER 10001

EXPOSE 5200

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5200/health', timeout=4)"

# One process only: the knowledge base lives in a single in-process JVM.
CMD ["waitress-serve", "--listen=0.0.0.0:5200", "--threads=8", "wsgi:app"]
