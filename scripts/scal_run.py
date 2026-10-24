#!/usr/bin/env python3
"""Scala metryki nadajnika, odbiornika i miary jakosci w JEDNA linie pomiary.jsonl.

Nic wiecej nie zapisuje. Pliki czastkowe czyta z katalogu roboczego (SCRATCH),
ktory run.sh tworzy w /tmp i kasuje po sobie - w results/ nie laduje nic poza
samym pomiary.jsonl.

Wymagane zmienne srodowiskowe:
    SCRATCH   - katalog z sender.json, receiver.json, vmaf.json, psnr.log, ssim.log
    JSONL     - docelowy plik pomiary.jsonl
Opcjonalne:
    RUN_TS, REF_MP4, OUT_MP4, NETEM_DELAY_MS, NETEM_LOSS_PCT, CICHO
"""

import json
import os
import subprocess
import sys

SCRATCH = os.environ["SCRATCH"]
JSONL = os.environ.get("JSONL", "results/pomiary.jsonl")


def commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip() or None
    except OSError:
        return None


def main():
    run_id = sys.argv[1]

    with open(f"{SCRATCH}/sender.json") as fh:
        s = json.load(fh)
    with open(f"{SCRATCH}/receiver.json") as fh:
        r = json.load(fh)

    with open(f"{SCRATCH}/quality.json") as fh:
        q = json.load(fh)
    for variable, actual in [("SENDER_CPUS", s.get("cpu_nadajnik")),
                             ("RECEIVER_CPUS", r.get("cpu_odbiornik"))]:
        requested = os.environ.get(variable)
        if requested and (actual is None or abs(float(requested) - actual) > 0.0005):
            raise RuntimeError(f"Niezgodny limit CPU: {variable}={requested}, cgroup={actual}")

    if s.get("transport", "raw") != r.get("transport", "raw"):
        raise RuntimeError("Sender/receiver transport mismatch")

    rekord = {
        "nr": run_id,
        "Batch": os.environ.get("SERIES_ID", "kontrola"),
        "jakosc_status": q["status"],
        "data": os.environ.get("RUN_TS"),
        "commit": commit(),
        "wideo": s["wideo"],
        "tryb_szyfru": s["tryb_szyfru"],
        "tryb_wymiany": s["tryb_wymiany"],
        "chunk_B": s["chunk_B"],
        "transport": s.get("transport", "raw"),
        "rtp_packet_B": s.get("rtp_packet_B"),
        "bajty_udp": s.get("bajty_udp"),
        "_max_datagram_B": s.get("_max_datagram_B"),
        "rtp_io": s.get("rtp_io"),
        "_rtcp": s.get("_rtcp"),
        "profil": s.get("profil"),
        "cpu_nadajnik": s.get("cpu_nadajnik"),
        "cpu_odbiornik": r.get("cpu_odbiornik"),
        "netem_opoznienie_ms": float(os.environ.get("NETEM_DELAY_MS", 0)),
        "netem_straty_%": float(os.environ.get("NETEM_LOSS_PCT", 0)),
        "gen_klucza_ns": s["gen_klucza_ns"],
        "wymiana_ns": s["wymiana_ns"],
        "wyprowadzenie_ns": s["wyprowadzenie_ns"],
        "szyfrowanie_ns": s["szyfrowanie_ns"],
        "deszyfrowanie_ns": r.get("deszyfrowanie_ns"),
        "pakiety_wyslane": s["pakiety_wyslane"],
        "pakiety_odebrane": r["pakiety_odebrane"],
        "opoznienie_ms": r.get("opoznienie_ms"),
        "klatki_ref": q["klatki_ref"],
        "klatki_wy": q["klatki_wy"],
        "vmaf": q.get("vmaf"),
        "psnr_dB": q.get("psnr_dB"),
        "ssim": q.get("ssim"),
        "_fps": q["fps"],
        "_jakosc_metoda": "pelna_transmisja; pary_klatek_Y4M; PSNR_z_MSE",
        "_kolejnosc_ok": r.get("_kolejnosc_ok"),
        # diagnostyka - importer pomija klucze z podkreslnikiem
        "_pakiety_odrzucone": r.get("pakiety_odrzucone"),
        "_opoznienie_p95_ms": r.get("_opoznienie_p95_ms"),
        "_opoznienie_n": r.get("_opoznienie_n"),
        "_domkniecie_odbiornika_ms": r.get("_domkniecie_odbiornika_ms"),
        "_transmisja_ns": s.get("transmisja_ns"),
        "_cpuset_nadajnik": s.get("_cpuset_nadajnik"),
        "_cpuset_odbiornik": r.get("_cpuset_odbiornik"),
        "_wersja_cryptography": s.get("_wersja_cryptography"),
    }

    os.makedirs(os.path.dirname(JSONL) or ".", exist_ok=True)
    with open(JSONL, "a") as fh:
        fh.write(json.dumps(rekord, ensure_ascii=False) + "\n")

    if os.environ.get("CICHO", "0") != "1":
        print(f"{run_id}: szyfr={rekord['tryb_szyfru']} "
              f"pakiety={rekord['pakiety_wyslane']}/{rekord['pakiety_odebrane']} "
              f"vmaf={rekord['vmaf']} opoznienie={rekord['opoznienie_ms']}ms")


main()
