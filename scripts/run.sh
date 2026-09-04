#!/usr/bin/env bash
set -uo pipefail

cd "$(dirname "$0")/.."
PROJ="$(pwd)"

# ------------------------------------------------------------ konfiguracja
FILENAME="${FILENAME:-leaves2}"
CIPHER="${CIPHER:-xor}"
KEX="${KEX:-wspoldzielony}"
CHUNK_B="${CHUNK_B:-1400}"
NETEM_DELAY_MS="${NETEM_DELAY_MS:-0}"
NETEM_LOSS_PCT="${NETEM_LOSS_PCT:-0}"
PROFIL="${PROFIL:-domyslny}"

if [ "$#" -gt 0 ]; then
    echo "BLAD: '$*' potraktowane jako argumenty pozycyjne, nie zmienne." >&2
    echo "Uzyj:  CIPHER=... KEX=... $0" >&2
    exit 1
fi

case "$CIPHER" in
    brak|xor|aes-ecb|aes-ctr|aes-cbc|aes-gcm|chacha20-poly1305) ;;
    *) echo "BLAD: nieznany szyfr '$CIPHER'" >&2; exit 1 ;;
esac
case "$KEX" in
    wspoldzielony|dh-768|dh-2048) ;;
    *) echo "BLAD: nieznany tryb wymiany '$KEX'" >&2; exit 1 ;;
esac

RUN_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%d-%H%M%S)-${CIPHER}-${KEX}}"

# ---------------------------------------------------------- katalog roboczy
# Wszystko posrednie laduje tutaj i znika przy wyjsciu ze skryptu, takze po
# przerwaniu Ctrl+C. W results/ zostaje wylacznie pomiary.jsonl.
SCRATCH="$(mktemp -d)"
sprzataj() {
    if [ "${ZACHOWAJ_PLIKI:-0}" = "1" ]; then
        echo "pliki posrednie: $SCRATCH" >&2
    else
        rm -rf "$SCRATCH"
    fi
}
trap sprzataj EXIT

SRC_MP4="${PROJ}/videos/${FILENAME}.mp4"
OUT_MP4="${SCRATCH}/out.mp4"
JSONL="${PROJ}/results/pomiary.jsonl"
mkdir -p "${PROJ}/results"

# Kontener zapisuje mp4 przez wspolny wolumen ./results:/results, wiec musi
# tam trafic pod unikalna nazwa - przenosimy go do SCRATCH zaraz po odbiorze.
OUT_W_KONTENERZE="/results/.tmp_${RUN_ID}.mp4"
OUT_NA_HOSCIE="${PROJ}/results/.tmp_${RUN_ID}.mp4"

FPS=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate \
      -of csv=p=0 "$SRC_MP4" | awk -F'/' '{if ($2) printf "%.0f", $1/$2; else print $1}')

echo "== ${RUN_ID} (szyfr=${CIPHER}, wymiana=${KEX}, profil=${PROFIL}, netem=${NETEM_DELAY_MS}ms/${NETEM_LOSS_PCT}%, fps=${FPS})"

docker compose up -d >/dev/null 2>&1
docker compose exec -T sender   pkill -f sender.py   2>/dev/null || true
docker compose exec -T receiver pkill -f receiver.py 2>/dev/null || true

# ------------------------------------------------------------------ netem
# UWAGA: netem wisi na NADAJNIKU, nie odbiorniku. Root qdisc ksztaltuje ruch
# WYCHODZACY, wiec dopiero tutaj opoznienie i straty dotykaja strumienia wideo
# (sender -> receiver). Na eth0 odbiornika ksztaltowalyby wylacznie odpowiedzi
# (ACK bariery, klucz publiczny DH), a opoznienie_ms zostaloby przy ~0.2 ms
# niezaleznie od ustawien - pomiar wygladalby na zepsuty.
docker compose exec -T sender tc qdisc del dev eth0 root 2>/dev/null || true
if [ "$NETEM_DELAY_MS" != "0" ] || [ "$NETEM_LOSS_PCT" != "0" ]; then
    docker compose exec -T sender tc qdisc add dev eth0 root netem \
        delay "${NETEM_DELAY_MS}ms" loss "${NETEM_LOSS_PCT}%"
fi

# ----------------------------------------------------------------- przebieg
# Metryki wracaja przez stdout, nie przez pliki w /results.
docker compose exec -T receiver env \
    RUN_ID="$RUN_ID" OUTPUT="$OUT_W_KONTENERZE" FPS="$FPS" CIPHER="$CIPHER" \
    KEX="$KEX" CHUNK_B="$CHUNK_B" PROFIL="$PROFIL" \
    python3 /app/receiver.py > "${SCRATCH}/receiver.raw" 2> "${SCRATCH}/receiver.err" &
RECEIVER_PID=$!

docker compose exec -T sender env \
    RUN_ID="$RUN_ID" INPUT="/videos/${FILENAME}.mp4" CIPHER="$CIPHER" \
    KEX="$KEX" CHUNK_B="$CHUNK_B" PROFIL="$PROFIL" \
    python3 /app/sender.py > "${SCRATCH}/sender.raw" 2> "${SCRATCH}/sender.err"
STATUS_SENDER=$?

wait "$RECEIVER_PID"
STATUS_RECEIVER=$?

docker compose exec -T sender tc qdisc del dev eth0 root 2>/dev/null || true

# ostatnia linia stdout to JSON z metrykami
tail -n 1 "${SCRATCH}/sender.raw"   > "${SCRATCH}/sender.json"
tail -n 1 "${SCRATCH}/receiver.raw" > "${SCRATCH}/receiver.json"

if [ "$STATUS_SENDER" != "0" ] || [ "$STATUS_RECEIVER" != "0" ] \
   || ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "${SCRATCH}/sender.json" 2>/dev/null \
   || ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "${SCRATCH}/receiver.json" 2>/dev/null
then
    echo "BLAD: przebieg nieudany (sender=$STATUS_SENDER receiver=$STATUS_RECEIVER)" >&2
    echo "--- sender.err ---" >&2;   tail -20 "${SCRATCH}/sender.err"   >&2
    echo "--- receiver.err ---" >&2; tail -20 "${SCRATCH}/receiver.err" >&2
    rm -f "$OUT_NA_HOSCIE"
    exit 1
fi

mv "$OUT_NA_HOSCIE" "$OUT_MP4" 2>/dev/null || {
    echo "BLAD: brak pliku wyjsciowego" >&2; exit 1; }

# ------------------------------------------------------------------ jakosc
ffmpeg -v error -i "$OUT_MP4" -pix_fmt yuv420p "${SCRATCH}/out.y4m" -y
ffmpeg -v error -i "$SRC_MP4" -pix_fmt yuv420p "${SCRATCH}/ref.y4m" -y

vmaf -r "${SCRATCH}/ref.y4m" -d "${SCRATCH}/out.y4m" --model version=vmaf_v0.6.1 \
     -o "${SCRATCH}/vmaf.json" --json --quiet

ffmpeg -hide_banner -loglevel error -i "$OUT_MP4" -i "$SRC_MP4" \
       -lavfi psnr=stats_file="${SCRATCH}/psnr.log" -f null -
ffmpeg -hide_banner -loglevel error -i "$OUT_MP4" -i "$SRC_MP4" \
       -lavfi ssim=stats_file="${SCRATCH}/ssim.log" -f null -

# ------------------------------------------------------------------ scalanie
SCRATCH="$SCRATCH" JSONL="$JSONL" RUN_TS="$RUN_TS" \
REF_MP4="$SRC_MP4" OUT_MP4="$OUT_MP4" \
NETEM_DELAY_MS="$NETEM_DELAY_MS" NETEM_LOSS_PCT="$NETEM_LOSS_PCT" \
    python3 scripts/scal_run.py "$RUN_ID" || {
        echo "BLAD: scalanie nieudane" >&2; exit 1; }