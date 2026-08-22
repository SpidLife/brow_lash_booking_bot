FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY . /app

RUN mkdir -p /app/data && useradd --create-home botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "run.py"]
