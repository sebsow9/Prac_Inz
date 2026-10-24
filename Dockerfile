FROM python:3.11-slim AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg iproute2 iputils-ping tcpdump procps \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "cryptography>=48.0.1" py-diffie-hellman==1.0.1 python-ffmpeg
COPY transport.py /opt/h264/transport.py
ENV PYTHONPATH=/opt/h264
WORKDIR /app
CMD ["sleep", "infinity"]
