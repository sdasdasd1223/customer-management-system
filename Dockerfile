FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home signing && mkdir -p /data && chown signing:signing /data
COPY --chown=signing:signing app.py auth.py access.py settings.py backup.py wsgi.py ./
COPY --chown=signing:signing static ./static
COPY --chown=signing:signing templates ./templates
USER signing
VOLUME ["/data"]
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "120", "--access-logfile", "-", "--access-logformat", "%(h)s %(t)s %(m)s %(U)s %(H)s %(s)s %(b)s", "wsgi:app"]
