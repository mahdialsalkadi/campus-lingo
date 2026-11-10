#!/usr/bin/env bash

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

LLAMA_PIDS=""

cleanup() {
    echo ""
    echo "🛑 جاري إيقاف جميع الخدمات..."
    kill $(jobs -p) 2>/dev/null || true
    [ -n "$LLAMA_PIDS" ] && kill $LLAMA_PIDS 2>/dev/null || true
    pkill -f uvicorn 2>/dev/null || true
    pkill -f "llama-server" 2>/dev/null || true
    sudo docker compose down 2>/dev/null || true
    sudo systemctl stop docker 2>/dev/null || true
    exit 0
}

trap cleanup SIGINT SIGTERM EXIT

# تحرير المنافذ القديمة وعمليات llama-server السابقة لضمان تطبيق الإعدادات
sudo fuser -k 8000/tcp 2>/dev/null || true
sudo fuser -k 3000/tcp 2>/dev/null || true
for port in 8081 8082 8083 8084; do
    sudo fuser -k ${port}/tcp 2>/dev/null || true
done
pkill -f "llama-server" 2>/dev/null || true
pkill -f "ollama serve" 2>/dev/null || true
sleep 1

echo "🚀 [1/5] تشغيل Docker وقواعد البيانات..."
sudo systemctl start docker
sudo docker compose up -d
sleep 3

# ---------------------------------------------------------------------------
# llama.cpp — مضبوط على AMD BC-250 (14GB VRAM)
#
#   -m            مسار نموذج GGUF (عائلة Qwen 3.5)
#   -ngl 99       كل طبقات النموذج على الـ GPU
#   -fa on        Flash Attention: يقلص عنق زجاجة عرض نطاق الذاكرة.
#                 هذه النسخة من llama.cpp (10199+) تتطلب قيمة صريحة on|off|auto؛
#                 "-fa" المجردة تجعل المحلل يبتلع العلم التالي (-ctk) فيُجهض التشغيل.
#   -ctk/-ctv q4_0  كاش KV بدقة 4 بت: نصف الذاكرة = سياق أكبر وتوليد أسرع
#                 (توصية Unsloth: إذا ظهر نص مشوّش استخدم --cache-type-k/-v bf16)
#   -c 16384      سياق 16k لكل مسار — طلب التوليد يحتاج ~2.5k برومبت + توليد كبير
#   -np 1         مسار واحد مخصص: 100% من حساب GPU وعرض نطاق الذاكرة للتهييم النشط
#   -b/-ub        دفعات عتادية لكل فئة (راجع مصفوفة الفئات أدناه)
#   -t 8          خيوط CPU
#   --mlock --no-mmap  تثبيت النموذج في الذاكرة الفعلية (لا page-fault أثناء التوليد)
#   --spec-type   التوليد التخميني: draft-mtp يتطلب GGUF برأس MTP مضمّن (Gemma 4)
#                 أو ملف مسودة منفصل عبر --model-draft (Qwen3.6 MTP). GGUFات
#                 Qwen3.5 القياسية لا تحتوي طبقات MTP — لذا الإعداد الافتراضي هو
#                 مسودة n-gram البسيطة التي تعمل مع أي نموذج (Unsloth: MTP يساّر
#                 30-50% تسريعاً فقط على نماذج مدربة بـ MTP).
#                 لا تُمرر --embeddings/--pooling أبداً مع أعلام التوليد التخميني:
#                 الخلط بينهما (تضمينات + توليد تخميني في نفس الخادم) يسبب
#                 core dump عند الإقلاع — خدمات التضمين تُدار داخل FastAPI.
#   EXTRA_FLAGS   أعلام إضافية تُمرر كما هي إلى llama-server (متغير بيئة)
#
#   كل فئة تُطلق llama-server خاصاً بها على منفذ ثابت:
#     0.8B -> 8081 | 2B -> 8082 | 4B -> 8083 | 9B -> 8084
#   الخلفية تختار المنفذ حسب الفئة المُرسلة مع كل عملية رفع (model_tier).
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# [1] اختيار الفئات: CLI (--model "1 3") أو LLAMA_TIERS env أو قائمة تفاعلية
# ---------------------------------------------------------------------------
MODEL_DIR="${MODEL_DIR:-models}"

# tier_key -> "port|gguf_path|parallel_slots|context|batch|ubatch|name|spec_flags"
# مصفوفة 16k: -np 1 لكل الفئات، مع -b/-ub مخصصة لكل حجم نموذج
#
# spec_flags: التوليد التخميني لكل فئة. Unsloth يوثّق MTP عبر
# "--spec-type draft-mtp --spec-draft-n-max 2" فقط لنماذج تحتوي رأس MTP
# (Gemma 4 GGUF محدّث أو GGUFات Qwen3.6-MTP مع --model-draft). نماذج
# Qwen3.5 القياسية (0.8B/2B/4B/9B) بلا طبقات MTP — وتشغيل draft-mtp عليها
# يُجهض العمل. لذلك الافتراضي هنا: مسودة n-gram بسيطة تعمل مع أي GGUF.
# لتفعيل MTP حقيقي: نزّل GGUF برأس MTP ومرّر
# LLAMA_SPEC_FLAGS="--spec-type draft-mtp --spec-draft-n-max 2"
DEFAULT_SPEC_FLAGS="${LLAMA_SPEC_FLAGS:---spec-type ngram-simple --spec-draft-n-max 8}"

# ---------------------------------------------------------------------------
# GBNF grammar enforcement + context calibration
#
#   --grammar-file x.gbnf    يحصر التوليد في مخطط Pydantic الصحيح حرفياً — لا
#                            إعادة محاولة بسبب JSON مكسور (صفر syntax retries).
#                            (--grammar يأخذ نص القواعد نفسه، وليس مسار ملف:
#                            تمرير مسار يُجهض الإقلاع بـ "expecting name".)
#                            الملفات تُولَّد أدناه من schema.py (مصدر الحقيقة
#                            الوحيد) عبر scripts/export_gbnf.py عند توفر venv.
#   -fa on                   Flash Attention بإشارة صريحة on|off|auto — النسخ
#                            الحديثة ترفض "-fa" المجردة (تلتهم العلم التالي).
#   -c = -np * 2048 * 8      معايرة ذاكرة السياق: Context = Parallel Slots x
#                            2048 (الافتراضي لمسار واحد: -np 1 -c 16384).
#                            تُحسب هنا من TIER_PARALLEL نفسه حتى لا تنفكّ.
#
# ملاحظة مهمة: قواعد الإقلاع تُقيّد كل completion لهذا الخادم. السوكروتيك
# (tutor hints) يبث نصاً حراً — لذلك الافتراضي هو json_schema لكل طلب، وتفعيل
# التقييد عند الإقلاع يكون اختيارياً عبر LLAMA_BOOT_GRAMMAR=1.
# ---------------------------------------------------------------------------
CHALLENGE_GRAMMAR="${CHALLENGE_GRAMMAR:-${PROJECT_ROOT}/services/core-ai/llm/grammars/challenge_bundle.gbnf}"
GBNF_FLAGS=""
if [ "${LLAMA_BOOT_GRAMMAR:-0}" = "1" ] && [ -f "$CHALLENGE_GRAMMAR" ]; then
    GBNF_FLAGS="--grammar-file $CHALLENGE_GRAMMAR"
    echo "📘 GBNF grammar (boot-enforced): $CHALLENGE_GRAMMAR"
elif [ "${LLAMA_BOOT_GRAMMAR:-0}" = "1" ]; then
    echo "📘 ملف GBNF غير موجود — سيتم الاعتماد على json_schema لكل طلب"
else
    echo "📘 GBNF: json_schema لكل طلب (فعّل LLAMA_BOOT_GRAMMAR=1 لتقييد الإقلاع)"
fi

# Auto-regenerate the grammar from schema.py so it can never drift from the
# payload contracts (best-effort; llama-server re-validates at boot anyway).
if [ -x "${PROJECT_ROOT}/services/core-ai/.venv/bin/python" ]; then
    if ! "${PROJECT_ROOT}/services/core-ai/.venv/bin/python" \
        "${PROJECT_ROOT}/services/core-ai/scripts/export_gbnf.py" \
        "$CHALLENGE_GRAMMAR" >/dev/null 2>&1; then
        echo "⚠️  تعذّر إعادة توليد قاعدة GBNF — سيُستخدم الملف الموجود كما هو"
    fi
fi
TIER_800M="${LLAMA_TIER1_PORT:-8081}|${LLAMA_TIER1_MODEL:-${MODEL_DIR}/qwen3.5-0.8b-instruct-q4_k_m.gguf}|1|16384|4096|2048|Qwen 3.5 0.8B|${LLAMA_TIER1_SPEC:-$DEFAULT_SPEC_FLAGS}"
TIER_2B="${LLAMA_TIER2_PORT:-8082}|${LLAMA_TIER2_MODEL:-${MODEL_DIR}/qwen3.5-2b-instruct-q4_k_m.gguf}|1|16384|4096|1024|Qwen 3.5 2B|${LLAMA_TIER2_SPEC:-$DEFAULT_SPEC_FLAGS}"
TIER_4B="${LLAMA_TIER3_PORT:-8083}|${LLAMA_TIER3_MODEL:-${MODEL_DIR}/qwen3.5-4b-instruct-q4_k_m.gguf}|1|16384|4096|1024|Qwen 3.5 4B (DeltaNet)|${LLAMA_TIER3_SPEC:-$DEFAULT_SPEC_FLAGS}"
TIER_9B="${LLAMA_TIER4_PORT:-8084}|${LLAMA_TIER4_MODEL:-${MODEL_DIR}/qwen3.5-9b-instruct-q4_k_m.gguf}|1|16384|2048|512|Qwen 3.5 9B|${LLAMA_TIER4_SPEC:-$DEFAULT_SPEC_FLAGS}"

SELECTED_TIERS=""

normalize_tier() {
    case "$1" in
        1|800m|800M|tiny)      echo "800M" ;;
        2|2b|2B|light)         echo "2B" ;;
        3|4b|4B|default|sweet) echo "4B" ;;
        4|9b|9B|heavy)         echo "9B" ;;
        *)                     echo "" ;;
    esac
}

resolve_tier_arg() {
    # Normalise a single tier argument; empty output = invalid.
    normalize_tier "$(echo "$1" | xargs 2>/dev/null || echo "$1")"
}

resolve_tiers_arg() {
    # Accept "1 3" | "13" | "800m,9b" | "all" -> ordered canonical tier list.
    local raw="$(echo "$1" | tr ',+' '  ' | xargs 2>/dev/null || echo "$1")"
    if [ "$raw" = "all" ] || [ "$raw" = "ALL" ]; then
        echo "800M 2B 4B 9B"
        return
    fi
    local compact="$(echo "$raw" | tr -d ' \t')"
    if [ -n "$compact" ] && [[ "$compact" =~ ^[0-9]+$ ]] && [ ${#compact} -ge 2 ]; then
        raw="$(echo "$compact" | sed 's/./& /g')"
    fi
    local resolved=""
    for token in $raw; do
        local tier="$(resolve_tier_arg "$token")"
        if [ -z "$tier" ]; then
            echo ""
            return
        fi
        case " $resolved " in
            *" $tier "*) ;;  # dedupe
            *) resolved="$resolved $tier" ;;
        esac
    done
    echo "$resolved" | xargs 2>/dev/null || echo "$resolved"
}

parse_tiers_value() {
    SELECTED_TIERS="$(resolve_tiers_arg "$1")"
    if [ -z "$SELECTED_TIERS" ]; then
        echo "❌ فئات نموذج غير صالحة: '$1' (المسموح: 1-4 أو 800m | 2b | 4b | 9b، مفصولة بمسافة، أو all)"
        exit 1
    fi
}

# 1) CLI arguments take precedence: ./run.sh --model 4b | --model "1 3" | --tiers=all
while [ $# -gt 0 ]; do
    case "$1" in
        --model|-m|--tiers)
            if [ $# -lt 2 ]; then
                echo "❌ $1 يحتاج قيمة (مثال: $1 3 أو $1 \"1 3\")"
                exit 1
            fi
            parse_tiers_value "$2"
            shift 2
            ;;
        --model=*|-m=*|--tiers=*)
            parse_tiers_value "${1#*=}"
            shift
            ;;
        *)
            shift
            ;;
    esac
done

# 2) Environment variables: LLAMA_TIERS="1 3" ./run.sh (MODEL_TIER as legacy alias)
if [ -z "$SELECTED_TIERS" ] && [ -n "${LLAMA_TIERS:-}" ]; then
    parse_tiers_value "$LLAMA_TIERS"
fi
if [ -z "$SELECTED_TIERS" ] && [ -n "${MODEL_TIER:-}" ]; then
    parse_tiers_value "$MODEL_TIER"
fi

# 3) Interactive menu with a 10-second timeout (default tier 3 / 4B). Skipped
#    when tiers were resolved from CLI/env, or when stdin is not a TTY.
if [ -z "$SELECTED_TIERS" ]; then
    if [ -t 0 ]; then
        echo ""
        echo "========================================="
        echo "  Select Model Tier(s) for llama-server:"
        echo "  Qwen 3.5 family — 0.8B | 2B | 4B | 9B"
        echo "========================================="
        echo "1) 800M - Ultra Fast  (High concurrency, instant quizzes)"
        echo "2) 2B   - Balanced    (Fast generation, lightweight)"
        echo "3) 4B   - Sweet Spot  (DeltaNet 3:1, high accuracy) [Default]"
        echo "4) 9B   - Deep Reasoning (Heavy academic lectures)"
        echo "========================================="
        printf "Enter choice [1-4] (Default 3; multi e.g. \"1 3\" or \"all\"): "
        read -r -t 10 TIER_CHOICE || TIER_CHOICE=""
        echo ""
        [ -z "$TIER_CHOICE" ] && TIER_CHOICE="3"
        SELECTED_TIERS="$(resolve_tiers_arg "$TIER_CHOICE")"
        if [ -z "$SELECTED_TIERS" ]; then
            echo "⚠️  اختيار غير صالح — سيتم استخدام الفئة الافتراضية (4B)"
            SELECTED_TIERS="4B"
        fi
    else
        SELECTED_TIERS="4B"
    fi
fi

echo "🦙 [2/5] تشغيل llama-server للفئات:${SELECTED_TIERS} على كرت الرسوميات..."

# ---------------------------------------------------------------------------
# فحص ملفات النماذج: الفئات التي ينقصها GGUF تُتخطى بتحذير، وإذا لم يبق أي
# فئة صالحة نخرج برسالة واضحة تحدد مسار التنزيل المتوقع.
# ---------------------------------------------------------------------------
STARTABLE_TIERS=""
for TIER in $SELECTED_TIERS; do
    TIER_META="$(eval echo \"\$TIER_${TIER}\")"
    TIER_MODEL_PATH="$(echo "$TIER_META" | cut -d'|' -f2)"
    TIER_NAME="$(echo "$TIER_META" | cut -d'|' -f7)"
    if [ -f "$TIER_MODEL_PATH" ]; then
        STARTABLE_TIERS="$STARTABLE_TIERS $TIER"
    else
        echo "⚠️  GGUF غير موجود للفئة ${TIER_NAME}: ${PROJECT_ROOT}/${TIER_MODEL_PATH}"
        echo "    نزّله وضعه في المسار أعلاه، أو اختر فئة أخرى: ./run.sh --model 2b"
    fi
done

if [ -z "$(echo $STARTABLE_TIERS | xargs 2>/dev/null)" ]; then
    echo ""
    echo "❌ لا يوجد أي ملف نموذج صالح من:${SELECTED_TIERS}"
    echo "   المسار الافتراضي: mkdir -p ${MODEL_DIR}"
    echo "   مثال: ${MODEL_DIR}/qwen3.5-4b-instruct-q4_k_m.gguf"
    exit 1
fi
SELECTED_TIERS="$STARTABLE_TIERS"

LLAMA_BIN="${LLAMA_BIN:-llama-server}"
if ! command -v "$LLAMA_BIN" >/dev/null 2>&1 && [ ! -x "$LLAMA_BIN" ]; then
    echo "❌ llama-server غير مثبت. ثبّته من https://github.com/ggml-org/llama.cpp (build with GGML_CUDA=1 or ROCm/HIP for BC-250)"
    exit 1
fi

# ---------------------------------------------------------------------------
# [3] إطلاق llama-server لكل فئة مختارة (منافذ 8081..8084) وانتظار جاهزيتها
# ---------------------------------------------------------------------------
LLAMA_GPU_LAYERS="${LLAMA_GPU_LAYERS:-99}"
LLAMA_THREADS="${LLAMA_THREADS:-8}"
# مهارة انتظار جاهزية كل نموذج (ثوانٍ) — التحميل على GPU يستغرق عادة أقل من دقيقة
LLAMA_HEALTH_TIMEOUT="${LLAMA_HEALTH_TIMEOUT:-300}"
LOG_DIR="$PROJECT_ROOT/logs"
mkdir -p "$LOG_DIR"
DEFAULT_TIER_ID=""

# MTP fallback: a GGUF without an embedded MTP head aborts startup when
# --spec-type draft-mtp is passed ("model doesn't contain MTP layers"), while
# n-gram self-speculation works on any model. So each tier is launched with its
# configured MTP flags and restarted once with this fallback if it dies.
FALLBACK_SPEC_FLAGS="${LLAMA_SPEC_FALLBACK:---spec-type ngram-simple --spec-draft-n-max 16}"  # n-gram: يعمل مع أي GGUF

show_log_tail() {
    echo "     آخر سطور السجل:"
    tail -n 15 "$TIER_LOG" 2>/dev/null | sed 's/^/       /'
}

# Launch the current tier with $1 as its speculative flags and wait until
# /health answers. Returns 1 if the process dies or the timeout elapses.
start_tier_server() {
    # مثبت وفق دليلي Unsloth (MTP Guide + Qwen3.5):
    #   - خادم التوليد لا يحمل أعلام التضمينات أبداً: --embeddings في llama.cpp
    #     الحديثة "وضع تضمينات فقط"، ودمجها مع التوليد التخميني يسبب
    #     Aborted (core dumped) — التضمينات أُزيلت من خوادم التوليد.
    #   -fa on: هذه النسخة (10199+) تشترط on|off|auto؛ "-fa" المجردة تلتهم
    #     العلم التالي وتُسقط الإقلاع (تم التحقق عملياً).
    #   -ctk/-ctv q4_0 لكاش KV مضغوط (يتطلب FA)؛ وإن ظهر نص مشوّش فالبديل
    #     الموثّق --cache-type-k bf16 --cache-type-v bf16.
    #   draft-mtp محجوز لنماذج MTP عبر LLAMA_SPEC_FLAGS؛ الافتراضي n-gram.
    "$LLAMA_BIN" \
        -m "$TIER_MODEL_PATH" \
        --host 0.0.0.0 \
        --port "$TIER_PORT" \
        -c "$TIER_CTX" \
        -b "$TIER_BATCH" \
        -ub "$TIER_UBATCH" \
        -fa on \
        -ctk q4_0 \
        -ctv q4_0 \
        -ngl "$LLAMA_GPU_LAYERS" \
        -np "$TIER_PARALLEL" \
        -t "$LLAMA_THREADS" \
        --mlock \
        --no-mmap \
        $GBNF_FLAGS \
        $1 \
        ${EXTRA_FLAGS:-} \
        > "$TIER_LOG" 2>&1 &
    TIER_PID=$!
    LLAMA_PIDS="$LLAMA_PIDS $TIER_PID"
    for i in $(seq 1 "$LLAMA_HEALTH_TIMEOUT"); do
        if curl -sf "http://127.0.0.1:${TIER_PORT}/health" >/dev/null 2>&1; then
            return 0
        fi
        if ! kill -0 "$TIER_PID" 2>/dev/null; then
            return 1
        fi
        if [ $((i % 10)) -eq 0 ]; then
            echo "   ⏳ ${TIER_NAME} ما زال يحمّل النموذج... (${i}s)"
        fi
        sleep 1
    done
    return 1
}

for TIER in $SELECTED_TIERS; do
    TIER_META="$(eval echo \"\$TIER_${TIER}\")"
    TIER_PORT="$(echo "$TIER_META" | cut -d'|' -f1)"
    TIER_MODEL_PATH="$(echo "$TIER_META" | cut -d'|' -f2)"
    TIER_PARALLEL="$(echo "$TIER_META" | cut -d'|' -f3)"
    TIER_CTX="$(echo "$TIER_META" | cut -d'|' -f4)"
    TIER_BATCH="$(echo "$TIER_META" | cut -d'|' -f5)"
    TIER_UBATCH="$(echo "$TIER_META" | cut -d'|' -f6)"
    TIER_NAME="$(echo "$TIER_META" | cut -d'|' -f7)"
    # MTP / speculative flags for THIS model tier (field 8).
    TIER_SPEC_FLAGS="$(echo "$TIER_META" | cut -d'|' -f8)"
    # سجل التشغيل في /tmp كما هو محدد في مصفوفة الأداء
    TIER_LOG="/tmp/llama_tier_${TIER}.log"

    echo "   ↳ ${TIER_NAME} على المنفذ ${TIER_PORT} (-np ${TIER_PARALLEL}) ..."
    echo "     السجل: ${TIER_LOG}"
    [ -n "$TIER_SPEC_FLAGS" ] && echo "     Speculative (MTP): ${TIER_SPEC_FLAGS}"

    if ! start_tier_server "$TIER_SPEC_FLAGS"; then
        # لا يوجد رأس MTP داخل هذا الـ GGUF؟ أعد الإقلاع مرة واحدة بمسودة n-gram.
        # مسودة n-gram تعمل مع أي GGUF؛ draft-mtp يتطلب رأس MTP أو --model-draft.
        if [ -n "$TIER_SPEC_FLAGS" ] && [ "$TIER_SPEC_FLAGS" != "$FALLBACK_SPEC_FLAGS" ]; then
            echo "   ⚠️  تعذّر تشغيل MTP لهذا الـ GGUF — إعادة الإقلاع بمسودة n-gram:"
            show_log_tail
            kill "$TIER_PID" 2>/dev/null || true
            TIER_SPEC_FLAGS="$FALLBACK_SPEC_FLAGS"
            if ! start_tier_server "$TIER_SPEC_FLAGS"; then
                echo "   ❌ llama-server للفئة ${TIER_NAME} فشل في الإقلاع:"
                show_log_tail
                exit 1
            fi
        else
            echo "   ❌ llama-server للفئة ${TIER_NAME} فشل في الإقلاع:"
            show_log_tail
            exit 1
        fi
    fi
    echo "   ✅ ${TIER_NAME} جاهز على المنفذ ${TIER_PORT}"

    [ -z "$DEFAULT_TIER_ID" ] && DEFAULT_TIER_ID="$TIER"
done

# ---------------------------------------------------------------------------
# [4] ربط الواجهة الخلفية بالفئات المختارة
# ---------------------------------------------------------------------------
echo "⚡ [3/5] تشغيل خادم FastAPI..."
FIRST_TIER_META="$(eval echo \"\$TIER_${DEFAULT_TIER_ID}\")"
export SELECTED_MODEL_TIER="$DEFAULT_TIER_ID"
export SELECTED_TIERS="$SELECTED_TIERS"
export SELECTED_MODEL_PATH="$(echo "$FIRST_TIER_META" | cut -d'|' -f2)"
export SELECTED_MODEL_PARALLEL="$(echo "$FIRST_TIER_META" | cut -d'|' -f3)"
export LLAMA_MODEL="default"
# نقاط النهاية لكل فئة (يجب أن تتفق مع قيم LLAMA_TIERn_PORT أعلاه)
export LLAMA_TIER1_PORT="${LLAMA_TIER1_PORT:-8081}"
export LLAMA_TIER2_PORT="${LLAMA_TIER2_PORT:-8082}"
export LLAMA_TIER3_PORT="${LLAMA_TIER3_PORT:-8083}"
export LLAMA_TIER4_PORT="${LLAMA_TIER4_PORT:-8084}"
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

echo "🌐 [4/5] تشغيل الواجهة..."
# Purge stale dev webpack chunks to prevent MODULE_NOT_FOUND runtime crashes
# ("Cannot find module './vendor-chunks/framer-motion.js' / '@opentelemetry.js'").
rm -rf apps/web/.next/server/vendor-chunks apps/web/.next/cache/webpack
(
    cd apps/web
    npm run dev -- -H 0.0.0.0
) &

echo ""
echo "🎉 المنظومة تعمل الآن! افتح: http://localhost:3000"
for TIER in $SELECTED_TIERS; do
    TIER_META="$(eval echo \"\$TIER_${TIER}\")"
    echo "🦙 llama-server (${TIER}): http://localhost:$(echo "$TIER_META" | cut -d'|' -f1) (OpenAI-compatible /v1)"
done
echo "🛑 للإغلاق: اضغط Ctrl + C"

wait
