#!/usr/bin/env bash

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

cleanup() {
    echo ""
    echo "🛑 جاري إيقاف جميع الخدمات..."
    kill $(jobs -p) 2>/dev/null || true
    pkill -f uvicorn 2>/dev/null || true
    sudo docker compose down 2>/dev/null || true
    sudo systemctl stop docker 2>/dev/null || true
    exit 0
}

trap cleanup SIGINT SIGTERM EXIT

# تحرير المنافذ القديمة
sudo fuser -k 8000/tcp 2>/dev/null || true
sudo fuser -k 3000/tcp 2>/dev/null || true

echo "🚀 [1/4] تشغيل Docker وقواعد البيانات..."
sudo systemctl start docker
sudo docker compose up -d
sleep 3

echo "🦙 [2/4] تشغيل Ollama على كرت الرسوميات..."
export OLLAMA_IGPU_ENABLE=1
ollama serve > /dev/null 2>&1 &
sleep 2

echo "⚡ [3/4] تشغيل خادم FastAPI (Qwen 3.5 9B)..."
export OLLAMA_MODEL="qwen3.5:9b"
export OLLAMA_BUNDLE_MODEL="qwen3.5:9b"
(
    cd services/core-ai
    source .venv/bin/activate
    uvicorn main:app --host 0.0.0.0 --port 8000
) &
sleep 3

# Detect LAN IP so other devices can reach the backend
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$LAN_IP" ]; then
    LAN_IP=$(ip route get 1 2>/dev/null | awk '{print $7; exit}')
fi
LAN_IP=${LAN_IP:-127.0.0.1}
export NEXT_PUBLIC_API_URL="http://${LAN_IP}:8000"
echo "   ↳ LAN IP: ${LAN_IP}  |  API URL: ${NEXT_PUBLIC_API_URL}"

echo "🌐 [4/4] تشغيل الواجهة..."
(
    cd apps/web
    npm run dev -- -H 0.0.0.0
) &

echo ""
echo "🎉 المنظومة تعمل الآن! افتح: http://localhost:3000"
echo "🛑 للإغلاق: اضغط Ctrl + C"

wait
