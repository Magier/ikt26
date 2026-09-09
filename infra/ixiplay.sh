#!/usr/bin/env bash
set -euo pipefail

# -------------------------
# iximiuz Kubernetes tunnel
# -------------------------

# SSH key path
KEY="$HOME/.ssh/iximiuz_labs_user"

# Resolve paths relative to this script rather than the caller's working directory.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

# Output kubeconfig
OUT_CONFIG="$REPO_ROOT/ixi-config.yaml"
PLAYGROUND_NAME="k8s-adv-emulation-1b038ba5"

# Parse --legacy flag
LEGACY=false
if [[ "${1:-}" == "--legacy" ]]; then
    LEGACY=true
    shift
fi

# Optional: pass PLAY_ID as first argument
PLAY_ID="${1:-}"

# PIDs for cleanup
PROXY_PID=""
TUNNEL_PID=""

cleanup() {
	status=$?
	cleaned_up=false
	if [[ -n "$PROXY_PID" ]] && kill -0 "$PROXY_PID" 2>/dev/null; then
		kill "$PROXY_PID" 2>/dev/null || true
		cleaned_up=true
	fi
	if [[ -n "$TUNNEL_PID" ]] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
		kill "$TUNNEL_PID" 2>/dev/null || true
		cleaned_up=true
	fi
	if [[ "$cleaned_up" == "true" ]]; then
		echo ""
		echo "🧹 Cleaned up proxy processes"
	fi
	trap - EXIT
	exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# -------------------------
# Resolve playground ID
# -------------------------

# Auto-resolve PLAY_ID if empty
if [[ -z "$PLAY_ID" ]]; then
    # CREATED is multi-word in recent labctl output, so STATUS is not a fixed column.
    # Match by exact playground name and RUNNING anywhere in the row.
	PLAY_ID=$(labctl playgrounds list | awk -v name="$PLAYGROUND_NAME" 'NR > 1 && $2 == name && toupper($0) ~ /(^|[[:space:]])RUNNING([[:space:]]|\()/ {print $1; exit}')
fi

# If still empty (no running instance), start a new playground
if [[ -z "$PLAY_ID" ]]; then
    echo "🚀 No running playground found — starting $PLAYGROUND_NAME..."
    PLAY_ID=$(labctl playground start $PLAYGROUND_NAME | tail -n1)
fi

if [[ -z "$PLAY_ID" ]]; then
    echo "❌ Could not determine PLAY_ID"
    exit 1
fi
echo "🛝 Using PLAY_ID=$PLAY_ID"


if [[ "$LEGACY" == "true" ]]; then

    # =========================================
    # LEGACY PATH (ssh-proxy + manual tunnel)
    # =========================================

    echo "[1/6] Starting ssh-proxy..."
    nohup labctl ssh-proxy "$PLAY_ID" < /dev/null > ssh-proxy.log 2>&1 &
    PROXY_PID=$!

    # Wait for port to appear in log
    SSH_PORT=""
    while [[ -z "$SSH_PORT" ]]; do
        if grep -q 'SSH proxy is running on' ssh-proxy.log; then
            SSH_PORT=$(grep 'SSH proxy is running on' ssh-proxy.log | head -n1 | awk '{print $6}')
            break
        fi
        sleep 1
    done

    if [[ -z "$SSH_PORT" ]]; then
        echo "❌ Could not detect SSH proxy port. Check ssh-proxy.log"
        exit 1
    fi

    echo "    ✅ SSH proxy running on port $SSH_PORT"

    SSH_BASE="ssh -i $KEY -p $SSH_PORT -o LogLevel=ERROR laborant@127.0.0.1"
    SCP_BASE="scp -i $KEY -P $SSH_PORT -o LogLevel=ERROR laborant@127.0.0.1"

    echo "[2/6] Fetching kubeconfig..."
    $SCP_BASE:~/.kube/config "$OUT_CONFIG"

    API_SERVER=$($SSH_BASE "kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'")
    API_HOST=$(echo "$API_SERVER" | sed -E 's|https://([^:/]+).*|\1|')
    echo "    ✅ API host inside playground: $API_HOST"

    echo "[4/6] Patching kubeconfig with tls-server-name..."
    sed -i.bak "s|server: .*|server: https://127.0.0.1:6443|" "$OUT_CONFIG"

    if ! grep -q "tls-server-name" "$OUT_CONFIG"; then
        awk -v host="$API_HOST" '
          /server: https:\/\/127\.0\.0\.1:6443/ {
            print
            print "    tls-server-name: " host
            next
          }
          {print}
        ' "$OUT_CONFIG" > "$OUT_CONFIG.tmp" && mv "$OUT_CONFIG.tmp" "$OUT_CONFIG"
    fi

    echo "[5/6] Starting SSH tunnel (press CTRL+C to exit)..."
    $SSH_BASE -N -L 6443:$API_HOST:6443 &
    TUNNEL_PID=$!

    echo "    👉 Ready to use:"
    echo "    kubectl --kubeconfig=$OUT_CONFIG get pods -A"

    sleep 1
    echo "[6/6] Testing kubectl connection..."
    if KUBECONFIG="$OUT_CONFIG" kubectl get nodes >/dev/null 2>&1; then
        echo "    ✅ Connected successfully!"
    else
        echo "    ⚠️ Connection test failed, check manually"
    fi

    wait $TUNNEL_PID

else

    # =========================================
    # MAIN PATH (labctl kube-proxy)
    # =========================================

    echo "[1/3] Starting labctl kube-proxy..."
    : > labctl-proxy.log
    labctl kube-proxy "$PLAY_ID" > labctl-proxy.log 2>&1 &
    PROXY_PID=$!

    # Wait for kubeconfig path in log (timeout 60s)
    KUBECONFIG_PATH=""
    ELAPSED=0
    while [[ -z "$KUBECONFIG_PATH" ]]; do
        if grep -q 'Kubeconfig saved to:' labctl-proxy.log; then
            KUBECONFIG_PATH=$(grep -A1 'Kubeconfig saved to:' labctl-proxy.log | tail -n1 | tr -d '[:space:]')
            break
        fi
        sleep 1
        ELAPSED=$((ELAPSED + 1))
        if [[ $ELAPSED -ge 60 ]]; then
            echo "❌ Timed out waiting for kubeconfig. Check labctl-proxy.log"
            exit 1
        fi
    done

    echo "    ✅ Kubeconfig at: $KUBECONFIG_PATH"

    # Wait for proxy to signal it is fully ready before copying and testing
    ELAPSED=0
    while ! grep -q 'Keeping port forwarding running' labctl-proxy.log; do
        sleep 1
        ELAPSED=$((ELAPSED + 1))
        if [[ $ELAPSED -ge 15 ]]; then
            echo "    ⚠️  Proxy ready signal not seen — proceeding anyway"
            break
        fi
    done

    echo "[2/3] Copying and patching kubeconfig..."
    cp "$KUBECONFIG_PATH" "$OUT_CONFIG"

    # The cert is valid for internal cluster IPs, not 127.0.0.1.
    # Extract a valid IP SAN from the live cert and set tls-server-name so kubectl can verify it.
    CLUSTER_NAME=$(kubectl config view --kubeconfig "$OUT_CONFIG" --minify -o jsonpath='{.clusters[0].name}')
    TLS_SERVER_NAME=$(echo | openssl s_client -connect 127.0.0.1:6443 2>/dev/null \
        | openssl x509 -noout -text 2>/dev/null \
        | grep -Eo 'IP Address:[0-9.]+' \
        | grep -v '127\.' \
        | head -n1 \
        | sed 's/IP Address://')
    if [[ -n "$TLS_SERVER_NAME" ]]; then
        kubectl config set-cluster "$CLUSTER_NAME" \
            --tls-server-name="$TLS_SERVER_NAME" \
            --kubeconfig "$OUT_CONFIG" >/dev/null
        echo "    ✅ Patched tls-server-name: $TLS_SERVER_NAME"
    else
        echo "    ⚠️  Could not extract tls-server-name from cert; TLS verification may fail"
    fi

    echo "    👉 Ready to use:"
    echo "    kubectl --kubeconfig=$OUT_CONFIG get pods -A"

    echo "[3/3] Testing kubectl connection..."
    if KUBECONFIG="$OUT_CONFIG" kubectl get nodes >/dev/null 2>&1; then
        echo "    ✅ Connected successfully! (press CTRL+C to stop)"
    else
        echo "    ⚠️ Connection test failed, check manually"
    fi

    FAIL_COUNT=0
    while kill -0 "$PROXY_PID" 2>/dev/null; do
        sleep 30
        if ! KUBECONFIG="$OUT_CONFIG" kubectl get nodes --request-timeout=8s >/dev/null 2>&1; then
            FAIL_COUNT=$((FAIL_COUNT + 1))
            echo "    ⚠️  connectivity check failed ($FAIL_COUNT/3)"
            if [[ $FAIL_COUNT -ge 3 ]]; then
                echo ""
                echo "⚠️  Playground appears to have expired — stopping"
                break
            fi
        else
            FAIL_COUNT=0
        fi
    done

fi
