set -euo pipefail

cd "$(dirname "$0")/.."

INPUT="${INPUT:-/videos/default.mp4}"

echo "Start Kontenerow"
docker compose up -d

echo "Receiver w tle"
docker compose exec -d receiver bash /app/receiver.sh

sleep 2

echo "Sender..."
docker compose exec -T sender env INPUT="$INPUT" bash /app/sender.sh

echo "sender zakonczyl, czekamy na zamkniecie receiver"
sleep 3
docker compose exec receiver pkill -INT ffmpeg || true
sleep 1

echo "wynik:"

ls -lh results || echo "brak wynikow"