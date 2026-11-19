FROM python:3.12.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -u 10001 -m warehouse
ARG APP_VERSION=dev
ARG GIT_COMMIT=unknown
ARG SOURCE_SHA256=unknown
LABEL org.opencontainers.image.title="warehouse-netops-dashboard" \
      org.opencontainers.image.version=$APP_VERSION \
      org.opencontainers.image.revision=$GIT_COMMIT \
      io.warehouse.source-sha256=$SOURCE_SHA256
ENV APP_VERSION=$APP_VERSION GIT_COMMIT=$GIT_COMMIT SOURCE_SHA256=$SOURCE_SHA256
COPY app/ ./app/
RUN mkdir -p /data && chown warehouse:warehouse /data
USER warehouse
EXPOSE 8080
CMD ["python", "-m", "app.main"]
