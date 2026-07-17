set -euo pipefail #wyjdz na bledzie, reference do nie istniejacej zmiennej, nieudany pipe

INPUT="${INPUT:-/videos/default.mp4}"
DEST="${DEST:-receiver}"
PORT="${PORT:-4444}"

#s - rozdzielczosc strumien
#i input - plik wejsciowy
# preset ultrafast - ustawienie szybkiego kodowania, kosztem rozmiaru pliku i jakości - ustawione z powodu szybkosci na czym nam zalezy przy szyfrowaniu
# codec libx264 - kodek do kompresji wideo, popularny i wydajny do H.264
# re - czytaj input w czasie rzeczywistym, strumienioweanie, inaczej plik zostanie wypluty na raz, a nie w czasie rzeczywistym


ffmpeg  -re \
        -i "$INPUT" -preset ultrafast \
        -codec libx264 \
        -tune zerolatency \
        -f rtp "rtp://${DEST}:${PORT}"

