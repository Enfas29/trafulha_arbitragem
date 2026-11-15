# Utiliza Python oficial pré-instalado, sem mise/pyenv.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY . /app
RUN mkdir -p /data

# Apenas monitorização e Paper Trading de leitura.
# Nenhuma API de execução de ordens é invocada por este processo.
CMD ["python", "monitor_v11.py"]
