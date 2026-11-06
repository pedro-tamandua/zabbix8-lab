FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && opentelemetry-bootstrap -a install

COPY app.py .

# Auto-instrumentação OpenTelemetry (Flask, requests, logging)
CMD ["opentelemetry-instrument", "python", "app.py"]
