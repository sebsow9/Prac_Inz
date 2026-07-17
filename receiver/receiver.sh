set -euo pipefail

PORT="${PORT:-4444}"
OUTPUT="${OUTPUT:-/results/output.mp4}"   

mkdir -p "$(dirname "$OUTPUT")"

echo "Starting receiver on port $PORT, saving to $OUTPUT"

# -y overwrite plik wyjsciowy
# procol_whitelist file,udp,rtp - zezwolenie na protokoly file, udp, rtp do odczytu strumienia
# copy - skopiowanie strumienia bez rekompresji, zachowuje oryginalna jakosc i format

ffmpeg -y \
        -protocol_whitelist file,udp,rtp \
        -i "/app/stream.sdp" \
        -c copy \
        "$OUTPUT"