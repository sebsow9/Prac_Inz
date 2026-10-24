#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="$(dirname "$0")/videos"
mkdir -p "$OUT_DIR"

gen() {
    local name="$1" size="$2" fps="$3" duration="$4"
    # lafvi - silnik generowania wideo, pozwala na tworzenie wideo z filtrów i efektów
    # medium - preset zestawu argumentow kodowania, balans miedzy szybkoscia a jakoscia, medium jest kompromisem miedzy szybkoscia a jakoscia 
    # pix_fmt yuv420p - format pikseli, yuv420p jest powszechnie stosowany w wideo, zapewnia dobra jakosc przy stosunkowo niskim rozmiarze pliku

    echo "Generuje wideo: $name, rozdzielczosc: $size, fps: $fps, czas trwania: $duration"
    ffmpeg -y -loglevel error \
        -f lavfi -i "testsrc=size=${size}:rate=${fps}:duration=${duration}" \
        -codec libx264 -preset medium -pix_fmt yuv420p \
        "$OUT_DIR/$name.mp4"
    }

gen default 1280x720 30 10
gen test_1080p 1920x1080 30 10

echo "gotowe, wyegnerowane wideo w katalogu: $OUT_DIR"
ls -lh "$OUT_DIR"