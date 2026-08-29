FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python -c "import database; database.init_db()" || true

EXPOSE 5000

CMD ["gunicorn", "-c", "gunicorn_config.py", "app:app"]
