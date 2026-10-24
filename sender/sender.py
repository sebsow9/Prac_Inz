"""Nadajnik: koduje H.264, szyfruje wybranym trybem i wysyla przez UDP.

Zbiera metryki i zapisuje je do /results/<RUN_ID>.sender.json.
Konfiguracja przez zmienne srodowiskowe (ustawiane w run.sh):
    RUN_ID, INPUT, CIPHER, KEX, CHUNK_B
"""

import json
import os
import socket
import struct
import subprocess
import time

from transport import validate_config, video_source

import cryptography
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
from cryptography.hazmat.primitives.asymmetric.mlkem import MLKEM768PrivateKey
from cryptography.hazmat.primitives.padding import PKCS7
from diffiehellman import DiffieHellman

RESULTS = os.environ.get("RESULTS_DIR", "/results")
RUN_ID = os.environ.get("RUN_ID", "run")
INPUT = os.environ.get("INPUT", "/videos/default.mp4")
CIPHER = os.environ.get("CIPHER", "xor")
KEX = os.environ.get("KEX", "wspoldzielony")
CHUNK_B = int(os.environ.get("CHUNK_B", "1388"))
TRANSPORT = os.environ.get("TRANSPORT", "rtp")
RTP_PACKET_B = int(os.environ.get("RTP_PACKET_B", "1400"))

DEST = os.environ.get("DEST", "receiver")
PORT_RX = 4445   # tu sluchá receiver
PORT_TX = 4446   # tu sluchamy my

# RTP_PACKET_B obejmuje naglowek RTP. Przy limicie 1400 B,
# naglowku pomiarowym 12 B i najwiekszym narzucie szyfru
# datagram UDP ma maksymalnie 1440 B (IPv4 MTU 1500).

# --------------------------------------------------------------- docker
def _czytaj(sciezka):
    try:
        with open(sciezka) as fh:
            return fh.read().strip()
    except OSError:
        return None


def limity_cpu():
    """Limity kontenera z cgroup (v2, z fallbackiem na v1).

    Czytamy z cgroup, a nie ze zmiennych srodowiskowych, bo interesuje nas to,
    co Docker naprawde zastosowal, a nie to, co bylo wpisane w compose.
    """
    cpu = cpuset = ram = None

    v2 = _czytaj("/sys/fs/cgroup/cpu.max")
    if v2:
        quota, period = v2.split()
        cpu = None if quota == "max" else round(int(quota) / int(period), 3)
        cpuset = _czytaj("/sys/fs/cgroup/cpuset.cpus.effective")
        m = _czytaj("/sys/fs/cgroup/memory.max")
        ram = None if m in (None, "max") else int(m)
    else:
        q = _czytaj("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        p = _czytaj("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if q and p and int(q) > 0:
            cpu = round(int(q) / int(p), 3)
        cpuset = _czytaj("/sys/fs/cgroup/cpuset/cpuset.cpus")
        m = _czytaj("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        if m and int(m) < 2**62:
            ram = int(m)

    if cpu is None and cpuset:          # brak kwoty -> licz przypiete rdzenie
        n = 0
        for czesc in cpuset.split(","):
            a, _, b = czesc.partition("-")
            n += int(b) - int(a) + 1 if b else 1
        cpu = float(n)

    return {"cpu": cpu, "cpuset": cpuset,
            "ram_MB": round(ram / 1024 / 1024) if ram else None}



# --------------------------------------------------------------- bariera
def bariera(sock):
    """Czeka az receiver bedzie gotowy. Wykonywana PRZED startem zegarow.

    Bez tego 'wymiana_ns' mierzy opoznienie startu procesu receivera,
    a nie czas wymiany klucza.
    """
    sock.settimeout(0.2)
    for _ in range(150):  # do 30 s
        sock.sendto(b"SYN", (DEST, PORT_RX))
        try:
            data, _addr = sock.recvfrom(64)
            if data == b"ACK":
                return
        except socket.timeout:
            continue
    raise RuntimeError("receiver nie odpowiedzial na bariere")


# --------------------------------------------------------- wymiana klucza
def wymiana_klucza(sock, tryb):
    """Zwraca (klucz, gen_ns, wymiana_ns, wyprowadzenie_ns).

    Trzy rozlaczne interwaly: generacja lokalna, wymiana po sieci,
    wyprowadzenie sekretu. Zaden nie zawiera czekania na drugi proces.
    """
    if tryb == "wspoldzielony":
        t0 = time.perf_counter_ns()
        klucz = os.urandom(32)
        t1 = time.perf_counter_ns()
        sock.sendto(klucz, (DEST, PORT_RX))
        sock.recvfrom(64)  # potwierdzenie od receivera
        t2 = time.perf_counter_ns()
        return klucz, t1 - t0, t2 - t1, 0

    if tryb in ("dh-768", "dh-2048"):
        grupa = 1 if tryb == "dh-768" else 14
        t0 = time.perf_counter_ns()
        dh = DiffieHellman(group=grupa)
        moj_pub = dh.get_public_key()
        t1 = time.perf_counter_ns()
        sock.sendto(moj_pub, (DEST, PORT_RX))

        peer_pub, _addr = sock.recvfrom(8192)
        t2 = time.perf_counter_ns()
        sekret = dh.generate_shared_key(peer_pub)
        t3 = time.perf_counter_ns()
        return sekret[:32], t1 - t0, t2 - t1, t3 - t2

    if tryb == "ml-kem-768":
        t0 = time.perf_counter_ns()
        klucz_prywatny = MLKEM768PrivateKey.generate()
        moj_pub = klucz_prywatny.public_key().public_bytes_raw()
        t1 = time.perf_counter_ns()
        sock.sendto(moj_pub, (DEST, PORT_RX))

        kapsula, _addr = sock.recvfrom(8192)
        t2 = time.perf_counter_ns()
        sekret = klucz_prywatny.decapsulate(kapsula)
        t3 = time.perf_counter_ns()
        return sekret, t1 - t0, t2 - t1, t3 - t2

    raise NotImplementedError(f"nieobslugiwany tryb wymiany: {tryb}")


# ---------------------------------------------------------------- szyfry
def zrob_szyfr(tryb, klucz):
    """Zwraca funkcje szyfrujaca chunk -> bytes (razem z IV/nonce/tagiem)."""
    if tryb == "brak":
        return lambda d: d

    if tryb == "xor":
        tab = bytes(i ^ klucz[0] for i in range(256))
        return lambda d: d.translate(tab)

    if tryb == "aes-ecb":
        def f(d):
            pad = PKCS7(128).padder()
            enc = Cipher(algorithms.AES(klucz[:16]), modes.ECB()).encryptor()
            return enc.update(pad.update(d) + pad.finalize()) + enc.finalize()
        return f

    if tryb == "aes-cbc":
        def f(d):
            iv = os.urandom(16)
            pad = PKCS7(128).padder()
            enc = Cipher(algorithms.AES(klucz[:16]), modes.CBC(iv)).encryptor()
            return iv + enc.update(pad.update(d) + pad.finalize()) + enc.finalize()
        return f

    if tryb == "aes-ctr":
        def f(d):
            iv = os.urandom(16)
            enc = Cipher(algorithms.AES(klucz[:16]), modes.CTR(iv)).encryptor()
            return iv + enc.update(d) + enc.finalize()
        return f

    if tryb == "aes-gcm":
        aead = AESGCM(klucz[:16])
        def f(d):
            n = os.urandom(12)
            return n + aead.encrypt(n, d, None)
        return f

    if tryb == "chacha20-poly1305":
        aead = ChaCha20Poly1305(klucz[:32])
        def f(d):
            n = os.urandom(12)
            return n + aead.encrypt(n, d, None)
        return f

    raise NotImplementedError(f"nieobslugiwany szyfr: {tryb}")


# ------------------------------------------------------------------ main
def main():
    validate_config(TRANSPORT, RTP_PACKET_B)
    os.makedirs(RESULTS, exist_ok=True)
    ffmpeg = [
        "ffmpeg", "-nostdin", "-re", "-i", INPUT, "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-bsf:v", "dump_extra=freq=keyframe", "-fps_mode", "passthrough",
    ]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", PORT_TX))
    try:
        bariera(sock)
        sock.settimeout(10)
        klucz, gen_ns, wym_ns, wyp_ns = wymiana_klucza(sock, KEX)
        ready, _ = sock.recvfrom(64)
        if ready != b"READY":
            raise RuntimeError("Receiver did not confirm video readiness")
        szyfruj = zrob_szyfr(CIPHER, klucz)
        pakiety = bajty_pt = bajty_udp = szyfrowanie_ns = max_udp = 0
        t_start = time.perf_counter_ns()
        log_path = f"{RESULTS}/{RUN_ID}.encoder.log"
        with video_source(ffmpeg, TRANSPORT, RTP_PACKET_B, CHUNK_B, log_path) as chunks:
            for seq, chunk in enumerate(chunks):
                t_a = time.perf_counter_ns()
                naglowek = struct.pack("!IQ", seq, t_a)
                payload = szyfruj(naglowek + chunk)
                szyfrowanie_ns += time.perf_counter_ns() - t_a
                if TRANSPORT == "rtp" and len(payload) > 1472:
                    raise RuntimeError("Encrypted UDP datagram exceeds IPv4 MTU 1500")
                sock.sendto(payload, (DEST, PORT_RX))
                pakiety += 1
                bajty_pt += len(chunk)
                bajty_udp += len(payload)
                max_udp = max(max_udp, len(payload))
        transmisja_ns = time.perf_counter_ns() - t_start
        for _ in range(5):
            sock.sendto(b"FIN", (DEST, PORT_RX))
    finally:
        sock.close()
    lim = limity_cpu()
    metryki = {
        "run_id": RUN_ID,
        "wideo": os.path.splitext(os.path.basename(INPUT))[0],
        "tryb_szyfru": CIPHER, "tryb_wymiany": KEX,
        "transport": TRANSPORT,
        "chunk_B": CHUNK_B if TRANSPORT == "raw" else None,
        "rtp_packet_B": RTP_PACKET_B if TRANSPORT == "rtp" else None,
        "rtp_io": "loopback-udp" if TRANSPORT == "rtp" else None,
        "_rtcp": "local-BYE-only" if TRANSPORT == "rtp" else None,
        "gen_klucza_ns": gen_ns, "wymiana_ns": wym_ns,
        "wyprowadzenie_ns": wyp_ns, "szyfrowanie_ns": szyfrowanie_ns,
        "pakiety_wyslane": pakiety, "bajty_jawne": bajty_pt,
        "bajty_udp": bajty_udp, "_max_datagram_B": max_udp,
        "transmisja_ns": transmisja_ns,
        "profil": os.environ.get("PROFIL", "domyslny"),
        "cpu_nadajnik": lim["cpu"], "_cpuset_nadajnik": lim["cpuset"],
        "_ram_nadajnik_MB": lim["ram_MB"],
        "_wersja_cryptography": cryptography.__version__,
    }
    print(json.dumps(metryki))


if __name__ == "__main__":
    main()
