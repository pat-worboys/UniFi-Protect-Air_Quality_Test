ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY requirements.txt /app/
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt

COPY run.py /app/run.py
CMD ["python3", "/app/run.py"]
