#!/usr/bin/env python3
"""Dopisuje przebiegi z results/pomiary.jsonl do arkusza 'Pomiary'.

Uzycie:
    python3 scripts/jsonl_to_xlsx.py                       # domyslne sciezki
    python3 scripts/jsonl_to_xlsx.py wyniki.jsonl ark.xlsx

Zasady:
  * dopasowanie po NAZWACH kolumn - kolejnosc kolumn w arkuszu moze sie zmieniac,
    a klucze ktorych w arkuszu nie ma (np. 'commit') sa po prostu pomijane;
  * kolumn liczonych formula skrypt nie dotyka, tylko rozciaga formuly w dol,
    jesli dopisywanych wierszy jest wiecej niz przygotowanych;
  * import jest idempotentny - przebieg o danym 'nr' nie zostanie dodany dwa razy,
    wiec mozna puszczac skrypt po kazdej serii na tym samym pliku jsonl;
  * przed zapisem powstaje kopia <arkusz>.bak.
"""

import json
import os
import re
import shutil
import sys

from openpyxl import Workbook, load_workbook

JSONL = sys.argv[1] if len(sys.argv) > 1 else "results/pomiary.jsonl"
XLSX = sys.argv[2] if len(sys.argv) > 2 else "pomiary.xlsx"
ARKUSZ = "Pomiary"

# Kolumny wyliczane formula w arkuszu - nigdy nie nadpisywane wartoscia.
LICZONE = ("straty_%", "szyfr_na_pakiet_ns", "klatki_ok")


def znajdz_naglowek(ws):
    """Zwraca (nr_wiersza, {nazwa_kolumny: indeks}). Szuka wiersza z 'nr'."""
    for r in range(1, 15):
        wartosci = {}
        for c in range(1, ws.max_column + 1):
            v = ws.cell(row=r, column=c).value
            if isinstance(v, str) and v.strip():
                wartosci[v.strip()] = c
        if "nr" in wartosci:
            return r, wartosci
    raise SystemExit(f"nie znalazlem wiersza naglowka w arkuszu '{ARKUSZ}'")


def pierwszy_wolny(ws, hr, kol_wejsciowe):
    """Pierwszy wiersz, w ktorym wszystkie kolumny wejsciowe sa puste."""
    r = hr + 1
    while True:
        if all(ws.cell(row=r, column=c).value in (None, "") for c in kol_wejsciowe):
            return r
        r += 1


def rozciagnij_formuly(ws, zrodlo, cel, kolumny):
    """Kopiuje formuly z wiersza 'zrodlo' do 'cel', przenumerowujac odwolania."""
    for c in kolumny:
        f = ws.cell(row=zrodlo, column=c).value
        if isinstance(f, str) and f.startswith("="):
            nowa = re.sub(r"(?<![A-Z0-9$])(\$?[A-Z]{1,2}\$?)(\d+)",
                          lambda m: f"{m.group(1)}{cel}" if int(m.group(2)) == zrodlo
                          else m.group(0), f)
            ws.cell(row=cel, column=c).value = nowa
            ws.cell(row=cel, column=c).number_format = \
                ws.cell(row=zrodlo, column=c).number_format


def main():
    if not os.path.exists(JSONL):
        raise SystemExit(f"brak pliku {JSONL}")

    rekordy = []
    with open(JSONL, encoding="utf-8") as fh:
        for i, linia in enumerate(fh, 1):
            linia = linia.strip()
            if not linia:
                continue
            try:
                rekordy.append(json.loads(linia))
            except json.JSONDecodeError as e:
                print(f"  pomijam linie {i} ({e})")

    if os.path.exists(XLSX):
        wb = load_workbook(XLSX)
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = ARKUSZ
        headers = list(dict.fromkeys(k for r in rekordy for k in r if not k.startswith("_")))
        if "nr" not in headers:
            raise SystemExit("Brak pomiarow do utworzenia arkusza")
        ws.append(headers)
        ws.freeze_panes = "A2"

    if ARKUSZ not in wb.sheetnames:
        raise SystemExit(f"arkusz '{ARKUSZ}' nie istnieje (sa: {wb.sheetnames})")
    ws = wb[ARKUSZ]

    hr, kolumny = znajdz_naglowek(ws)
    kol_liczone = [kolumny[n] for n in LICZONE if n in kolumny]
    kol_wejsciowe = [c for n, c in kolumny.items() if n not in LICZONE]

    # juz zaimportowane przebiegi
    kol_nr = kolumny["nr"]
    istniejace = set()
    for r in range(hr + 1, ws.max_row + 1):
        v = ws.cell(row=r, column=kol_nr).value
        if v:
            istniejace.add(str(v))

    wiersz = pierwszy_wolny(ws, hr, kol_wejsciowe)

    # wzor formul: dowolny wiersz, ktory je ma - po usunieciu wierszy w Excelu
    # formuly potrafia zaczynac sie dopiero w srodku zakresu
    wzor = None
    if kol_liczone:
        for r in range(hr + 1, ws.max_row + 1):
            if isinstance(ws.cell(row=r, column=kol_liczone[0]).value, str):
                wzor = r
                break

    dodane, pominiete, ostrzezenia = 0, 0, []
    nieznane = set()

    for rek in rekordy:
        nr = str(rek.get("nr", ""))
        if nr in istniejace:
            pominiete += 1
            continue

        for klucz, wartosc in rek.items():
            if klucz.startswith("_"):
                continue          # pola diagnostyczne zostaja w jsonie
            if klucz in LICZONE:
                continue          # liczy Excel
            if klucz not in kolumny:
                nieznane.add(klucz)
                continue
            if wartosc is None:
                continue          # puste zostaje puste
            ws.cell(row=wiersz, column=kolumny[klucz]).value = wartosc

        # odtworz formuly zawsze, gdy w docelowym wierszu ich nie ma
        if wzor and kol_liczone and not isinstance(
                ws.cell(row=wiersz, column=kol_liczone[0]).value, str):
            rozciagnij_formuly(ws, wzor, wiersz, kol_liczone)

        ref, wy = rek.get("klatki_ref"), rek.get("klatki_wy")
        if ref is not None and wy is not None and ref != wy:
            ostrzezenia.append(f"{nr}: klatki {ref} -> {wy}")

        istniejace.add(nr)
        wiersz += 1
        dodane += 1

    if dodane:
        if os.path.exists(XLSX):
            shutil.copyfile(XLSX, XLSX + ".bak")
        temporary = XLSX + ".tmp.xlsx"
        wb.save(temporary)
        os.replace(temporary, XLSX)

    print(f"dodane: {dodane}, juz byly: {pominiete}")
    if nieznane:
        print("klucze bez kolumny w arkuszu (pominiete): " + ", ".join(sorted(nieznane)))
    if ostrzezenia:
        print("NIEZGODNA liczba klatek - te przebiegi odrzuc przy analizie jakosci:")
        for o in ostrzezenia:
            print("  " + o)
    print(f"kopia zapasowa: {XLSX}.bak")
    print("Podsumowanie przeliczy sie przy otwarciu pliku w Excelu/LibreOffice.")


main()
