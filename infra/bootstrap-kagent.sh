#!/usr/bin/env bash
set -euo pipefail

# -------------------------------------------------------------------
# Workshop cluster bootstrap
#
# Installs:
#   1. Rancher Local Path Provisioner
#   2. Makes local-path the default StorageClass
#   3. kagent with the minimal profile
#
# Everything is intentionally local/ephemeral. No LLM credentials
# are required at this stage.
# -------------------------------------------------------------------

KAGENT_VERSION="${KAGENT_VERSION:-0.10.0-rc6}"
LOCAL_PATH_VERSION="${LOCAL_PATH_VERSION:-v0.0.37}"

KAGENT_NAMESPACE="kagent"
LOCAL_PATH_NAMESPACE="local-path-storage"

log() {
    echo
    echo "==> $*"
}

die() {
    echo
    echo "ERROR: $*" >&2
    exit 1
}

# -------------------------------------------------------------------
# Prerequisites
# -------------------------------------------------------------------

command -v kubectl >/dev/null 2>&1 \
    || die "kubectl is required"

command -v helm >/dev/null 2>&1 \
    || die "helm is required"

kubectl cluster-info >/dev/null 2>&1 \
    || die "Cannot connect to Kubernetes"

log "Kubernetes cluster"

kubectl version --short 2>/dev/null || kubectl version --client

# -------------------------------------------------------------------
# Local Path Provisioner
# -------------------------------------------------------------------

log "Installing local-path-provisioner ${LOCAL_PATH_VERSION}"

kubectl apply -f \
    "https://raw.githubusercontent.com/rancher/local-path-provisioner/${LOCAL_PATH_VERSION}/deploy/local-path-storage.yaml"

log "Waiting for local-path-provisioner"

kubectl rollout status \
    deployment/local-path-provisioner \
    -n "${LOCAL_PATH_NAMESPACE}" \
    --timeout=120s

# -------------------------------------------------------------------
# Default StorageClass
# -------------------------------------------------------------------

log "Making local-path the default StorageClass"

kubectl patch storageclass local-path \
    --type=merge \
    -p '{"metadata":{"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'

log "Storage classes"

kubectl get storageclass

# Sanity check
if ! kubectl get storageclass local-path >/dev/null 2>&1; then
    die "local-path StorageClass was not created"
fi

# -------------------------------------------------------------------
# kagent
# -------------------------------------------------------------------

log "Installing kagent ${KAGENT_VERSION}"

# If the CLI is already installed, use it.
# Otherwise install it.
#
# IMPORTANT:
# For a production workshop image, I recommend baking the exact
# kagent CLI into the image rather than downloading it at boot.
if ! command -v kagent >/dev/null 2>&1; then
    log "kagent CLI not found; installing"

    curl -fsSL \
        https://raw.githubusercontent.com/kagent-dev/kagent/refs/heads/main/scripts/get-kagent \
        | bash
fi

log "kagent CLI version"

kagent version || true

# -------------------------------------------------------------------
# Install kagent
# -------------------------------------------------------------------

log "Installing kagent with minimal profile"

KAGENT_DEFAULT_MODEL_PROVIDER=ollama \ kagent install \
    --profile minimal \
    --timeout 5m

# -------------------------------------------------------------------
# Wait for core components
# -------------------------------------------------------------------

log "Waiting for kagent pods"

kubectl wait \
    --for=condition=Ready \
    pod \
    --all \
    -n "${KAGENT_NAMESPACE}" \
    --timeout=300s

# -------------------------------------------------------------------
# Verify PostgreSQL
# -------------------------------------------------------------------

log "Checking PostgreSQL"

kubectl get pvc -n "${KAGENT_NAMESPACE}"

kubectl wait \
    --for=condition=Ready \
    pod \
    -l app.kubernetes.io/component=database \
    -n "${KAGENT_NAMESPACE}" \
    --timeout=300s

# -------------------------------------------------------------------
# Final status
# -------------------------------------------------------------------

log "kagent installation complete"

echo
kubectl get pods -n "${KAGENT_NAMESPACE}"
echo
kubectl get pvc -n "${KAGENT_NAMESPACE}"
echo
kubectl get storageclass

echo
echo "=============================================================="
echo " Workshop environment ready"
echo "=============================================================="
echo
echo " kagent namespace:"
echo "   ${KAGENT_NAMESPACE}"
echo
echo " kagent version:"
echo "   ${KAGENT_VERSION}"
echo
echo " storage:"
echo "   local-path (default)"
echo
echo " Next:"
echo "   kubectl get agents -n ${KAGENT_NAMESPACE}"
echo