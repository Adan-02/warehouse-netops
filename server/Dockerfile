FROM python:3.12.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -u 10001 -m warehouse
COPY app/ ./app/
RUN mkdir -p /data && chown warehouse:warehouse /data
USER warehouse
EXPOSE 8080
CMD ["python", "-m", "app.main"]
