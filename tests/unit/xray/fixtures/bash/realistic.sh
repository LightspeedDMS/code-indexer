#!/usr/bin/env bash
# Realistic Bash: deployment script with functions, error handling, and logging

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LOG_FILE="/tmp/deploy-$(date +%Y%m%d-%H%M%S).log"
readonly MAX_RETRIES=3
readonly RETRY_DELAY=5

log() {
    local level="$1"; shift
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$level] $*" | tee -a "$LOG_FILE"
}

log_info()  { log "INFO"  "$@"; }
log_warn()  { log "WARN"  "$@"; }
log_error() { log "ERROR" "$@"; }

die() {
    log_error "$@"
    exit 1
}

check_deps() {
    local missing=()
    for cmd in git docker docker-compose curl jq; do
        if ! command -v "$cmd" &>/dev/null; then
            missing+=("$cmd")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        die "Missing dependencies: ${missing[*]}"
    fi
}

retry() {
    local cmd=("$@")
    local attempt=0
    while [[ $attempt -lt $MAX_RETRIES ]]; do
        if "${cmd[@]}"; then
            return 0
        fi
        attempt=$(( attempt + 1 ))
        if [[ $attempt -lt $MAX_RETRIES ]]; then
            log_warn "Attempt $attempt/$MAX_RETRIES failed, retrying in ${RETRY_DELAY}s..."
            sleep "$RETRY_DELAY"
        fi
    done
    return 1
}

get_current_version() {
    git describe --tags --abbrev=0 2>/dev/null || echo "0.0.0"
}

build_image() {
    local tag="$1"
    local context="${2:-.}"
    log_info "Building Docker image: $tag"
    docker build \
        --tag "$tag" \
        --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --build-arg VCS_REF="$(git rev-parse --short HEAD)" \
        --label "version=$tag" \
        "$context"
}

push_image() {
    local tag="$1"
    log_info "Pushing image: $tag"
    retry docker push "$tag"
}

run_healthcheck() {
    local url="$1"
    local timeout="${2:-60}"
    local elapsed=0
    local interval=5

    log_info "Waiting for $url to become healthy (timeout: ${timeout}s)"
    while [[ $elapsed -lt $timeout ]]; do
        if curl --silent --fail --max-time 5 "$url" &>/dev/null; then
            log_info "Service is healthy after ${elapsed}s"
            return 0
        fi
        sleep "$interval"
        elapsed=$(( elapsed + interval ))
    done
    log_error "Service did not become healthy within ${timeout}s"
    return 1
}

deploy() {
    local env="$1"
    local version="$2"
    local dry_run="${3:-false}"

    log_info "Deploying version $version to $env"

    local compose_file="docker-compose.${env}.yml"
    if [[ ! -f "$compose_file" ]]; then
        die "Compose file not found: $compose_file"
    fi

    if [[ "$dry_run" == "true" ]]; then
        log_info "Dry run mode - skipping actual deployment"
        return 0
    fi

    export APP_VERSION="$version"
    docker-compose -f "$compose_file" pull
    docker-compose -f "$compose_file" up -d --remove-orphans

    local health_url
    health_url="$(jq -r ".services.app.environment.HEALTH_URL // \"http://localhost:8080/health\"" "$compose_file" 2>/dev/null || echo "http://localhost:8080/health")"
    run_healthcheck "$health_url" 120 || die "Deployment health check failed"

    log_info "Deployment of $version to $env completed successfully"
}

cleanup_old_images() {
    local keep="${1:-5}"
    log_info "Cleaning up old images (keeping latest $keep)"
    docker images --format "{{.Repository}}:{{.Tag}}" \
        | grep -v "<none>" \
        | sort -V \
        | head -n "-$keep" \
        | xargs -r docker rmi || true
}

main() {
    local env="${1:-staging}"
    local version
    version="$(get_current_version)"
    local dry_run="${2:-false}"

    log_info "Starting deployment: env=$env version=$version dry_run=$dry_run"

    check_deps
    build_image "myapp:${version}" .
    push_image "myapp:${version}"
    deploy "$env" "$version" "$dry_run"
    cleanup_old_images 5

    log_info "All done!"
}

main "$@"
