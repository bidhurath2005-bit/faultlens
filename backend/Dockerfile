FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY alembic.ini .
COPY migrations ./migrations
COPY app ./app
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app && chown -R app /app
USER app
EXPOSE 8000 9464
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
