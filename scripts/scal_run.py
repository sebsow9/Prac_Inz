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
import re
import subprocess
import sys

SCRATCH = os.environ["SCRATCH"]
JSONL = os.environ.get("JSONL", "results/pomiary.jsonl")


def klatki(sciezka):
    """Liczba zdekodowanych klatek wideo. None gdy pliku nie ma."""
    if not sciezka or not os.path.exists(sciezka):
        return None
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", sciezka],
        capture_output=True, text=True,
    ).stdout.strip()
    return int(out) if out.isdigit() else None


def commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True).stdout.strip() or None
    except OSError:
        return None


def vmaf(sciezka):
    if not os.path.exists(sciezka):
        return None
    try:
        with open(sciezka) as fh:
            return round(json.load(fh)["pooled_metrics"]["vmaf"]["mean"], 4)
    except (ValueError, KeyError):
        return None


def srednia_z_logu(sciezka, wzorzec):
    """Srednia metryki z per-klatkowego logu ffmpeg."""
    if not os.path.exists(sciezka):
        return None
    wartosci = []
    with open(sciezka) as fh:
        for linia in fh:
            m = re.search(wzorzec, linia)
            if m:
                try:
                    wartosci.append(float(m.group(1)))
                except ValueError:
                    pass
    return round(sum(wartosci) / len(wartosci), 4) if wartosci else None


def main():
    run_id = sys.argv[1]

    with open(f"{SCRATCH}/sender.json") as fh:
        s = json.load(fh)
    with open(f"{SCRATCH}/receiver.json") as fh:
        r = json.load(fh)

    rekord = {
        "nr": run_id,
        "data": os.environ.get("RUN_TS"),
        "commit": commit(),
        "wideo": s["wideo"],
        "tryb_szyfru": s["tryb_szyfru"],
        "tryb_wymiany": s["tryb_wymiany"],
        "chunk_B": s["chunk_B"],
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
        "klatki_ref": klatki(os.environ.get("REF_MP4")),
        "klatki_wy": klatki(os.environ.get("OUT_MP4")),
        "vmaf": vmaf(f"{SCRATCH}/vmaf.json"),
        "psnr_dB": srednia_z_logu(f"{SCRATCH}/psnr.log", r"psnr_avg:([0-9.]+)"),
        "ssim": srednia_z_logu(f"{SCRATCH}/ssim.log", r"All:([0-9.]+)"),
        # diagnostyka - importer pomija klucze z podkreslnikiem
        "_pakiety_odrzucone": r.get("pakiety_odrzucone"),
        "_opoznienie_p95_ms": r.get("_opoznienie_p95_ms"),
        "_opoznienie_n": r.get("_opoznienie_n"),
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