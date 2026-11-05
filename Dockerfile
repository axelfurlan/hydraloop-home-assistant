FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hydraloop_mqtt.py .

RUN useradd --system --no-create-home hydraloop
USER hydraloop

CMD ["python", "-u", "hydraloop_mqtt.py"]
