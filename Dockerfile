FROM python:3.11-slim

# ffmpeg - kodowanie /dekodowanie wideo
# iproute2 - tc/netem - narzedzia do konfiguracji sieci
# iputils-ping - tcpdump- diagnostyka
RUN apt-get update \
 && apt-get install -y ffmpeg iproute2 iputils-ping tcpdump \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --upgrade pip \
 && pip install \
      cryptography==48.0.0 \
      py-diffie-hellman==1.0.1 \
      python-ffmpeg

WORKDIR /app

CMD ["sleep", "infinity"]