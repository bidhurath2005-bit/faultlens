# One image, five services: SERVICE_MODULE selects which app uvicorn runs.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY common /app/common
RUN pip install /app/common
COPY user_service /app/user_service
COPY order_service /app/order_service
COPY payment_service /app/payment_service
COPY inventory_service /app/inventory_service
COPY notification_service /app/notification_service
COPY traffic_generator /app/traffic_generator
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app && chown -R app /app
USER app
ENV SERVICE_MODULE=order_service.main:app PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health',timeout=2)" || exit 1
CMD ["sh", "-c", "exec uvicorn ${SERVICE_MODULE} --host 0.0.0.0 --port ${PORT} --no-access-log"]
