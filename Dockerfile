FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 8080: Flask 试跑页；8001: TFTP 服务 stats；69/udp: TFTP
EXPOSE 8080/tcp 8001/tcp 69/udp

# 默认启动网页；compose 中 tftp 服务会把 command 覆盖为 server.py
CMD ["python", "-u", "web.py"]
