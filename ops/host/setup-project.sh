#!/usr/bin/env bash
# Prepara l'host per un progetto con un solo comando (#643): esegue nell'ordine setup-devloop.sh,
# setup-agent-user.sh, setup-supervisor.sh e, se il progetto ha il coordinatore (--coordinator, o
# l'overlay lo dichiara con [apps.coordinator] o [roles.coordinator]), setup-coordinator.sh, tutti
# con gli stessi parametri. Prima tutti i --dry-run; se uno fallisce si ferma, senza cambiare
# nulla. Poi, con una sola conferma, le esecuzioni vere, ciascuna con --yes, fermandosi al primo
# errore. Alla fine elenca in un solo blocco i passi a mano rimasti, raccolti dall'output degli
# script, con i passi interattivi che restano sempre (login delle CLI, app, avvio).
# Gli script singoli restano, e fanno lo stesso: questo li compone soltanto. Idempotente come
# loro: dopo una modifica dei parametri (per esempio APPS) basta rieseguirlo. Non scrive segreti.
# Con --dry-run fa solo il primo giro.

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lib.sh
. "$OPS_DIR/lib.sh"

usage() {
    say "Uso: sudo bash $0 --params FILE [--coordinator] [--dry-run] [--yes]"
    say
    say '  --coordinator   anche setup-coordinator.sh, anche se l'"'"'overlay non dichiara ancora il'
    say '                  coordinatore'
    say "$OPS_COMMON_HELP"
}

WITH_COORDINATOR=0
while [ $# -gt 0 ]; do
    case $1 in
        -h | --help) usage; exit 0 ;;
        --coordinator) WITH_COORDINATOR=1; shift ;;
        *) ops_common_arg "$@" || exit 2; shift "$OPS_SHIFT" ;;
    esac
done

# Gli utenti dei ruoli li verificano gli script dei ruoli (#643; setup-devloop.sh).
OPS_DEFER_ROLE_USERS=1
ops_load_params
ops_check_platform
require_root
PARAMS_FILE=$(cd "$(dirname "$PARAMS_FILE")" && pwd)/$(basename "$PARAMS_FILE")

SCRIPTS=(setup-devloop.sh setup-agent-user.sh setup-supervisor.sh)
if [ "$WITH_COORDINATOR" = 1 ] || overlay_declares_coordinator "$STATE_PATH/config.local.toml"; then
    SCRIPTS+=(setup-coordinator.sh)
fi

OUT=$(mktemp -d) || die 'non riesce a creare una directory temporanea'
trap 'rm -rf "$OUT"' EXIT

# run_setup SCRIPT ARG...: lo script con i parametri del progetto, con l'output anche in
# $OUT/SCRIPT; torna il suo codice di uscita.
run_setup() {
    local script=$1
    shift
    bash "$OPS_DIR/host/$script" --params "$PARAMS_FILE" "$@" 2>&1 | tee "$OUT/$script"
}

step "Prova di tutti gli script (--dry-run): ${SCRIPTS[*]}"
for script in "${SCRIPTS[@]}"; do
    step "$script --dry-run"
    run_setup "$script" --dry-run \
        || die "$script --dry-run non riesce: nessuno script eseguito, niente cambiato"
done

# remaining: i passi a mano degli script (le righe "da fare a mano:" del loro output), poi quelli
# che restano sempre, in un solo blocco.
remaining() {
    local script line
    step 'Passi a mano rimasti'
    for script in "${SCRIPTS[@]}"; do
        [ -f "$OUT/$script" ] || continue
        while IFS= read -r line; do
            say "  [$script] ${line#  da fare a mano: }"
        done < <(grep '^  da fare a mano: ' "$OUT/$script" || true)
    done
    say '  e, se non sono già fatti (ops/README.md, docs/devloop/quickstart.md):'
    say "  - le app dell'insieme: create (ops/github/create-apps.html), installate su $REPO, e le chiavi nelle key_dir"
    say "  - [apps] e gli altri valori nell'overlay $STATE_PATH/config.local.toml, il file di progetto e il primo pin"
    say "  - i login delle CLI: dell'utente degli agent $AGENT_USER, del supervisore $SUPERVISOR_USER e, se c'è, del coordinatore $COORDINATOR_USER"
    say "  - la verifica: sudo bash $OPS_DIR/host/check-host.sh --params $PARAMS_FILE"
    say "  - l'avvio: sudo systemctl start $SERVICE_NAME, poi sudo bash $OPS_DIR/host/start-role.sh --params $PARAMS_FILE supervisor (e coordinator)"
}

if [ "$DRY_RUN" = 1 ]; then
    remaining
    step 'Fatto (--dry-run: niente eseguito)'
    exit 0
fi

step "Esecuzione: ${SCRIPTS[*]}"
confirm "Eseguire ora ${#SCRIPTS[@]} script per $REPO, con i parametri $PARAMS_FILE?" \
    || die "interrotto dall'operatore: niente eseguito"
for script in "${SCRIPTS[@]}"; do
    step "$script"
    run_setup "$script" --yes || die "$script non riesce: gli script dopo non sono stati eseguiti; correggere e rieseguire $(basename "$0")"
done

remaining
step 'Fatto'
