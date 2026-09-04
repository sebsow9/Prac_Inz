#!/usr/bin/env bash
# Seria pomiarowa: N iteracji x szyfr x wymiana klucza x kombinacja CPU.
#
# USTAWIENIA ponizej -> ./runner_main.sh -> gotowe.
# Kazde ustawienie mozna tez nadpisac z linii polecen:
#   ITERACJE=5 SZYFRY="aes-gcm xor" ./runner_main.sh
#   TAK=1 ./runner_main.sh          # bez pytania o potwierdzenie
#   DRY_RUN=1 ./runner_main.sh      # sam plan
#
# Wznawianie: przebieg, ktorego 'nr' jest juz w results/pomiary.jsonl,
# zostaje pominiety - po przerwaniu uruchom skrypt ponownie.

# ============================== USTAWIENIA ==============================
ITERACJE="${ITERACJE:-3}"
SZYFRY="${SZYFRY:-brak xor aes-ecb aes-ctr aes-cbc aes-gcm chacha20-poly1305}"
WYMIANY="${WYMIANY:-wspoldzielony dh-768 dh-2048}"
CPU_NAD="${CPU_NAD:-0.2}"          # limity CPU nadajnika
CPU_ODB="${CPU_ODB:-0.2}"          # limity CPU odbiornika (iloczyn = kombinacje)
FILENAME="${FILENAME:-leaves2}"
NETEM_DELAY_MS="${NETEM_DELAY_MS:-0.5}"  # opoznienie sieci dla CALEJ serii
NETEM_LOSS_PCT="${NETEM_LOSS_PCT:-0.5}"  # straty pakietow dla CALEJ serii
LIMIT_S="${LIMIT_S:-300}"              # twardy limit czasu na jeden przebieg
# ========================================================================

set -uo pipefail          # bez -e: nieudany przebieg nie ubija serii

SKRYPT="$(cd "$(dirname "$0")" && pwd)"
if   [ -f "$SKRYPT/docker-compose.yml" ];    then PROJ="$SKRYPT"
elif [ -f "$SKRYPT/../docker-compose.yml" ]; then PROJ="$(cd "$SKRYPT/.." && pwd)"
else
    echo "BLAD: nie znalazlem docker-compose.yml ani w $SKRYPT, ani poziom wyzej." >&2
    exit 1
fi
cd "$PROJ"

LOG="${PROJ}/results/seria.log"
BLEDY="${PROJ}/results/seria_bledy.log"
JSONL="${PROJ}/results/pomiary.jsonl"
mkdir -p "${PROJ}/results"

# ------------------------------------------------------------ kontrola wstepna
blad=0
[ -x "${PROJ}/scripts/run.sh" ] || {
    echo "BLAD: brak scripts/run.sh albo nie ma prawa wykonywania (chmod +x)" >&2; blad=1; }
[ -f "${PROJ}/videos/${FILENAME}.mp4" ] || {
    echo "BLAD: brak videos/${FILENAME}.mp4" >&2; blad=1; }
command -v ffprobe >/dev/null || { echo "BLAD: brak ffprobe" >&2; blad=1; }
command -v vmaf    >/dev/null || { echo "BLAD: brak vmaf" >&2; blad=1; }
command -v timeout >/dev/null || { echo "BLAD: brak 'timeout' (coreutils)" >&2; blad=1; }
docker compose ps >/dev/null 2>&1 || {
    echo "BLAD: 'docker compose' niedostepne albo brak uprawnien" >&2; blad=1; }
[ "$blad" = "0" ] || exit 1

# --------------------------------------------------------------- plan serii
n_szyfr=$(echo "$SZYFRY" | wc -w);  n_kex=$(echo "$WYMIANY" | wc -w)
n_cn=$(echo "$CPU_NAD" | wc -w);    n_co=$(echo "$CPU_ODB" | wc -w)
kombinacje=$(( n_cn * n_co ))
komorki=$(( n_szyfr * n_kex * kombinacje ))
przebiegi=$(( komorki * ITERACJE ))

CZAS_WIDEO=$(ffprobe -v error -show_entries format=duration -of csv=p=0 \
             "${PROJ}/videos/${FILENAME}.mp4" | cut -d. -f1)
CZAS_WIDEO="${CZAS_WIDEO:-10}"
SEKUNDY=$(( przebiegi * (CZAS_WIDEO + 12) ))

echo "=============================================================="
echo " Projekt ....... $PROJ"
echo " Wideo ......... ${FILENAME}.mp4 (${CZAS_WIDEO}s)"
echo " Iteracje ...... $ITERACJE"
echo " Szyfry ........ $n_szyfr ($SZYFRY)"
echo " Wymiany ....... $n_kex ($WYMIANY)"
echo " Kombinacje CPU  $kombinacje (nadajnik: $CPU_NAD | odbiornik: $CPU_ODB)"
echo " Netem ......... delay=${NETEM_DELAY_MS}ms loss=${NETEM_LOSS_PCT}%"
echo " Przebiegi ..... $przebiegi   (limit ${LIMIT_S}s/przebieg)"
echo " Szacowany czas  ~$(( SEKUNDY / 3600 )) h $(( (SEKUNDY % 3600) / 60 )) min"
echo "                 (zaklada kodowanie w czasie rzeczywistym - przy niskich"
echo "                  limitach CPU bedzie dluzej)"
echo "=============================================================="

if [ "${DRY_RUN:-0}" = "1" ]; then
    echo "(DRY_RUN=1 - plan kolejnosci, bez uruchamiania)"
    for it in $(seq 1 "$ITERACJE"); do
      for scpu in $CPU_NAD; do for rcpu in $CPU_ODB; do
        for kex in $WYMIANY; do for szyfr in $SZYFRY; do
          echo "  i${it}_n${scpu}-o${rcpu}_${kex}_${szyfr}_d${NETEM_DELAY_MS}_s${NETEM_LOSS_PCT}"
        done; done
      done; done
    done
    exit 0
fi

if [ -t 0 ] && [ "${TAK:-0}" != "1" ]; then
    read -r -p "Kontynuowac? [t/N] " odp
    case "$odp" in t|T|y|Y) ;; *) echo "przerwano"; exit 0 ;; esac
fi

# --------------------------------------------------------------- przebiegi
: > "$BLEDY"
start=$(date +%s)
zrobione=0
pominiete=0
nieudane=0

uruchom() {
    local run_id="$1" profil="$2" scpu="$3" rcpu="$4" kex="$5" szyfr="$6"

    if grep -qF "\"nr\": \"${run_id}\"" "$JSONL" 2>/dev/null; then
        pominiete=$(( pominiete + 1 ))
        return 0
    fi

    printf "\r  [%s] >> %-58s" "$(date +%H:%M:%S)" "$run_id"

    if RUN_ID="$run_id" PROFIL="$profil" \
       SENDER_CPUS="$scpu" RECEIVER_CPUS="$rcpu" \
       CIPHER="$szyfr" KEX="$kex" FILENAME="$FILENAME" \
       NETEM_DELAY_MS="$NETEM_DELAY_MS" NETEM_LOSS_PCT="$NETEM_LOSS_PCT" \
       timeout "$LIMIT_S" ./scripts/run.sh >> "$LOG" 2>&1 < /dev/null
    then
        return 0
    fi
    local kod=$?

    echo "" >&2
    if [ "$kod" = "124" ]; then
        echo "$(date -u +%H:%M:%S) TIMEOUT (${LIMIT_S}s) ${run_id}" | tee -a "$BLEDY" >&2
    else
        echo "$(date -u +%H:%M:%S) NIEUDANY (kod $kod) ${run_id}" | tee -a "$BLEDY" >&2
    fi
    if [ "$nieudane" = "0" ]; then
        echo "--- ostatnie 15 linii ${LOG}:" >&2
        tail -15 "$LOG" >&2
        echo "---" >&2
    fi
    nieudane=$(( nieudane + 1 ))
    return 1
}

# Iteracja jest petla zewnetrzna - dryf w czasie rozklada sie rownomiernie
# na wszystkie warianty. Kombinacja CPU zaraz pod nia, bo jej zmiana wymaga
# odtworzenia kontenerow.

for it in $(seq 1 "$ITERACJE"); do
  for scpu in $CPU_NAD; do
    for rcpu in $CPU_ODB; do
        profil="n${scpu}-o${rcpu}"

        printf "\r  [%s] odtwarzam kontenery: %-42s" "$(date +%H:%M:%S)" "$profil"
        if ! SENDER_CPUS="$scpu" RECEIVER_CPUS="$rcpu" \
             docker compose up -d --force-recreate >> "$LOG" 2>&1; then
            echo "BLAD: nie udalo sie odtworzyc kontenerow dla ${profil}" >&2
            tail -15 "$LOG" >&2
            exit 1
        fi

        for kex in $WYMIANY; do
          for szyfr in $SZYFRY; do
            run_id="i${it}_${profil}_${kex}_${szyfr}_d${NETEM_DELAY_MS}_s${NETEM_LOSS_PCT}"
            if uruchom "$run_id" "$profil" "$scpu" "$rcpu" "$kex" "$szyfr"; then
                zrobione=$(( zrobione + 1 ))
            fi

            minelo=$(( $(date +%s) - start ))
            printf "\r  %d/%d  ok=%d pominiete=%d bledy=%d  %02d:%02d:%02d %20s" \
                $(( zrobione + pominiete + nieudane )) "$przebiegi" \
                "$zrobione" "$pominiete" "$nieudane" \
                $(( minelo / 3600 )) $(( (minelo % 3600) / 60 )) $(( minelo % 60 )) ""
          done
        done
    done
  done
done
echo

echo "Zakonczono: ok=$zrobione pominiete=$pominiete bledy=$nieudane"
echo "Import:  python3 scripts/jsonl_xlsx.py results/pomiary.jsonl pomiary.xlsx"
[ "$nieudane" -gt 0 ] && echo "Nieudane przebiegi: $BLEDY"
exit 0