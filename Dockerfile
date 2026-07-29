FROM python:3.12-slim

WORKDIR /app
COPY . /app
ENV PYTHONPATH=/app/src

CMD ["python", "scripts/harness_entrypoint.py"]

