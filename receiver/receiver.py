"""Odbiornik: odbiera UDP, odszyfrowuje i zapisuje strumien H.264 do mp4.

Zapisuje metryki do /results/<RUN_ID>.receiver.json.
Konfiguracja: RUN_ID, OUTPUT, FPS, CIPHER, KEX, CHUNK_B
"""

import json
import os
import socket
import struct
import subprocess
import time

from transport import validate_config, validate_rtp, video_sink

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.mlkem import MLKEM768PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
from cryptography.hazmat.primitives.padding import PKCS7
from diffiehellman import DiffieHellman
import statistics

RESULTS = os.environ.get("RESULTS_DIR", "/results")
RUN_ID = os.environ.get("RUN_ID", "run")
OUTPUT = os.environ.get("OUTPUT", "/results/output.mp4")
FPS = os.environ.get("FPS", "30")
CIPHER = os.environ.get("CIPHER", "xor")
KEX = os.environ.get("KEX", "wspoldzielony")
CHUNK_B = int(os.environ.get("CHUNK_B", "1388"))
TRANSPORT = os.environ.get("TRANSPORT", "rtp")
RTP_PACKET_B = int(os.environ.get("RTP_PACKET_B", "1400"))

PORT_RX = 4445
PORT_TX = 4446
BUF = max(CHUNK_B, RTP_PACKET_B, 8192) + 256  # zapas na IV/nonce/tag/padding

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

def bariera(sock):
    """Odpowiada na SYN od sendera dopoki nie dostanie czegos innego."""
    while True:
        data, addr = sock.recvfrom(BUF)
        if data == b"SYN":
            sock.sendto(b"ACK", (addr[0], PORT_TX))
            continue
        return data, addr  # pierwszy pakiet niebedacy barierą


def wymiana_klucza(sock, tryb, pierwszy, addr):
    if tryb == "wspoldzielony":
        sock.sendto(b"OK", (addr[0], PORT_TX))
        return pierwszy

    if tryb in ("dh-768", "dh-2048"):
        grupa = 1 if tryb == "dh-768" else 14
        dh = DiffieHellman(group=grupa)
        sock.sendto(dh.get_public_key(), (addr[0], PORT_TX))
        return dh.generate_shared_key(pierwszy)[:32]

    if tryb == "ml-kem-768":
        klucz_publiczny = MLKEM768PublicKey.from_public_bytes(pierwszy)
        sekret, kapsula = klucz_publiczny.encapsulate()
        sock.sendto(kapsula, (addr[0], PORT_TX))
        return sekret

    raise NotImplementedError(f"nieobslugiwany tryb wymiany: {tryb}")


def zrob_deszyfr(tryb, klucz):
    if tryb == "brak":
        return lambda d: d

    if tryb == "xor":
        tab = bytes(i ^ klucz[0] for i in range(256))
        return lambda d: d.translate(tab)

    if tryb == "aes-ecb":
        def f(d):
            dec = Cipher(algorithms.AES(klucz[:16]), modes.ECB()).decryptor()
            unpad = PKCS7(128).unpadder()
            return unpad.update(dec.update(d) + dec.finalize()) + unpad.finalize()
        return f

    if tryb == "aes-cbc":
        def f(d):
            iv, ct = d[:16], d[16:]
            dec = Cipher(algorithms.AES(klucz[:16]), modes.CBC(iv)).decryptor()
            unpad = PKCS7(128).unpadder()
            return unpad.update(dec.update(ct) + dec.finalize()) + unpad.finalize()
        return f

    if tryb == "aes-ctr":
        def f(d):
            iv, ct = d[:16], d[16:]
            dec = Cipher(algorithms.AES(klucz[:16]), modes.CTR(iv)).decryptor()
            return dec.update(ct) + dec.finalize()
        return f

    if tryb == "aes-gcm":
        aead = AESGCM(klucz[:16])
        return lambda d: aead.decrypt(d[:12], d[12:], None)

    if tryb == "chacha20-poly1305":
        aead = ChaCha20Poly1305(klucz[:32])
        return lambda d: aead.decrypt(d[:12], d[12:], None)

    raise NotImplementedError(f"nieobslugiwany szyfr: {tryb}")

def podsumuj_opoznienia(wartosci_ns):
    """Mediana, p95 i srednia z listy opoznien jednokierunkowych, w ms.

    Mediana, nie srednia, jest wartoscia wiodaca: pojedyncze przelaczenie
    kontekstu potrafi dorzucic setki mikrosekund i rozjechac srednia,
    nie mowiac nic o koszcie szyfru.
    """
    if not wartosci_ns:
        return None, None, None
    w = sorted(wartosci_ns)
    p95 = w[min(len(w) - 1, int(0.95 * len(w)))]
    return (statistics.median(w) / 1e6, p95 / 1e6, statistics.fmean(w) / 1e6)

def main():
    validate_config(TRANSPORT, RTP_PACKET_B)
    os.makedirs(RESULTS, exist_ok=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
    sock.bind(("", PORT_RX))
    sock.settimeout(60)
    try:
        pierwszy, addr = bariera(sock)
        klucz = wymiana_klucza(sock, KEX, pierwszy, addr)
        odszyfruj = zrob_deszyfr(CIPHER, klucz)
        log_path = f"{RESULTS}/{RUN_ID}.decoder.log"
        pakiety = bledy = deszyfrowanie_ns = 0
        t_pierwszy = t_ostatni = t_koniec = None
        opoznienia = []
        oczekiwany_seq = 0
        kolejnosc_ok = True
        first_rtp_timestamp = None
        zakonczenie = "timeout"
        with video_sink(TRANSPORT, OUTPUT, FPS, log_path) as deliver:
            sock.sendto(b"READY", (addr[0], PORT_TX))
            sock.settimeout(500)
            while True:
                try:
                    data, _a = sock.recvfrom(BUF)
                    t_odbior = time.perf_counter_ns()
                except socket.timeout:
                    t_koniec = time.perf_counter_ns()
                    break
                if data == b"FIN":
                    zakonczenie = "FIN"
                    t_koniec = t_odbior
                    break
                if t_pierwszy is None:
                    t_pierwszy = t_odbior
                t_ostatni = t_odbior
                try:
                    t_a = time.perf_counter_ns()
                    jawne = odszyfruj(data)
                    deszyfrowanie_ns += time.perf_counter_ns() - t_a
                    seq, t_wyslania = struct.unpack("!IQ", jawne[:12])
                    packet = jawne[12:]
                    if TRANSPORT == "rtp":
                        validate_rtp(packet, RTP_PACKET_B)
                        if first_rtp_timestamp is None:
                            first_rtp_timestamp = struct.unpack("!I", packet[4:8])[0]
                except (InvalidTag, ValueError, struct.error):
                    bledy += 1
                    continue
                if seq != oczekiwany_seq:
                    kolejnosc_ok = False
                oczekiwany_seq = seq + 1
                opoznienia.append(t_odbior - t_wyslania)
                deliver(packet)
                pakiety += 1
        domkniecie_ms = (time.perf_counter_ns() - t_koniec) / 1e6
    except socket.timeout:
        raise SystemExit("receiver: sender did not finish the handshake in time")
    finally:
        sock.close()
    lim = limity_cpu()
    med, p95, sr = podsumuj_opoznienia(opoznienia)
    if med is not None and med < 0:
        med = p95 = sr = None
    metryki = {
        "run_id": RUN_ID, "transport": TRANSPORT,
        "_rtp_first_timestamp": first_rtp_timestamp,
        "rtp_io": "loopback-udp" if TRANSPORT == "rtp" else None,
        "pakiety_odebrane": pakiety, "pakiety_odrzucone": bledy,
        "_kolejnosc_ok": kolejnosc_ok, "_zakonczenie": zakonczenie,
        "_domkniecie_odbiornika_ms": round(domkniecie_ms, 4),
        "odbior_ns": (t_ostatni - t_pierwszy) if t_pierwszy else None,
        "opoznienie_ms": round(med, 4) if med is not None else None,
        "_opoznienie_p95_ms": round(p95, 4) if p95 is not None else None,
        "_opoznienie_sr_ms": round(sr, 4) if sr is not None else None,
        "_opoznienie_n": len(opoznienia),
        "deszyfrowanie_ns": deszyfrowanie_ns,
        "cpu_odbiornik": lim["cpu"], "_cpuset_odbiornik": lim["cpuset"],
        "_ram_odbiornik_MB": lim["ram_MB"],
    }
    print(json.dumps(metryki))


if __name__ == "__main__":
    main()
