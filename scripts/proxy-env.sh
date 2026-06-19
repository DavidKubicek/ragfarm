# scripts/proxy-env.sh — load repo-root .env and normalize proxy variables.
#
# SOURCE this (do not execute) at the top of any build command block that
# fetches from the network — pip installs, HuggingFace snapshot_download, and
# `docker compose` (so image pulls and container env inherit the proxy):
#
#     source scripts/proxy-env.sh
#     pip install -U FlagEmbedding ...
#     docker compose -f infra/compose.yaml up -d qdrant
#
# What it does:
#   1. Loads VAR=VALUE lines from repo-root .env (the file lives only on the
#      build host; it is gitignored). No eval/word-splitting: each line is read
#      literally, so values may contain spaces/special chars without quoting.
#   2. Exports HTTP_PROXY/HTTPS_PROXY/NO_PROXY and their lowercase mirrors
#      (curl/wget/git/pip variously want one case or the other).
#   3. GUARANTEES NO_PROXY contains the always-internal hosts, merged with
#      whatever .env supplies. This is the safety net: the whole stack is a mesh
#      of 127.0.0.1 / docker-network calls (ingester->:8090/:6333, curl probes,
#      search_corpus->Qdrant, Open WebUI->:8080, mcpo->MCP, containers->qdrant /
#      host.docker.internal). If a proxy were set without these in NO_PROXY,
#      every internal call would be routed at the proxy and the build would break.
#
# Idempotent and safe to source repeatedly. Does not rely on `set -e`.

# Resolve the repo root from this script's location (works regardless of CWD).
__ragfarm_proxy_self="${BASH_SOURCE[0]:-$0}"
__ragfarm_root="$(cd "$(dirname "$__ragfarm_proxy_self")/.." 2>/dev/null && pwd)"
__ragfarm_env="${__ragfarm_root}/.env"

# 1. Load .env (VAR=VALUE), skipping blanks and # comments. No eval.
if [ -f "$__ragfarm_env" ]; then
    while IFS= read -r __line || [ -n "$__line" ]; do
        __line="${__line%$'\r'}"                       # strip CRLF if present
        case "$__line" in
            ''|\#*) continue ;;                        # blank or comment
        esac
        case "$__line" in
            [A-Za-z_]*=*) ;;                           # must look like VAR=...
            *) continue ;;
        esac
        __key="${__line%%=*}"
        __val="${__line#*=}"
        export "$__key=$__val"
    done < "$__ragfarm_env"
else
    echo "[proxy-env] note: no .env at $__ragfarm_env — proceeding without proxy" >&2
fi

# 2. Mirror case for whichever of the proxy vars are set.
[ -n "${HTTP_PROXY:-}"  ] && export http_proxy="$HTTP_PROXY"
[ -n "${http_proxy:-}"  ] && export HTTP_PROXY="$http_proxy"
[ -n "${HTTPS_PROXY:-}" ] && export https_proxy="$HTTPS_PROXY"
[ -n "${https_proxy:-}" ] && export HTTPS_PROXY="$https_proxy"

# 3. NO_PROXY: mandatory internal-host baseline, merged with .env's value, deduped.
__ragfarm_no_proxy_baseline="localhost,127.0.0.1,0.0.0.0,::1,host.docker.internal,qdrant"
__ragfarm_no_proxy_in="${NO_PROXY:-${no_proxy:-}}"
__ragfarm_no_proxy_merged="$__ragfarm_no_proxy_baseline${__ragfarm_no_proxy_in:+,$__ragfarm_no_proxy_in}"

# dedupe while preserving order
__ragfarm_no_proxy_out=""
__ragfarm_oldifs="$IFS"; IFS=','
for __h in $__ragfarm_no_proxy_merged; do
    [ -z "$__h" ] && continue
    case ",$__ragfarm_no_proxy_out," in
        *",$__h,"*) ;;                                 # already present
        *) __ragfarm_no_proxy_out="${__ragfarm_no_proxy_out:+$__ragfarm_no_proxy_out,}$__h" ;;
    esac
done
IFS="$__ragfarm_oldifs"
export NO_PROXY="$__ragfarm_no_proxy_out"
export no_proxy="$__ragfarm_no_proxy_out"

# Concise status to stderr (kept out of stdout so it never pollutes captured data).
if [ -n "${HTTP_PROXY:-}${HTTPS_PROXY:-}" ]; then
    echo "[proxy-env] proxy active: HTTP_PROXY=${HTTP_PROXY:-<unset>} HTTPS_PROXY=${HTTPS_PROXY:-<unset>}" >&2
    echo "[proxy-env] NO_PROXY=$NO_PROXY" >&2
else
    echo "[proxy-env] no HTTP(S)_PROXY in .env — direct connections (NO_PROXY still set)" >&2
fi

unset __ragfarm_proxy_self __ragfarm_root __ragfarm_env __line __key __val \
      __ragfarm_no_proxy_baseline __ragfarm_no_proxy_in __ragfarm_no_proxy_merged \
      __ragfarm_no_proxy_out __ragfarm_oldifs __h
