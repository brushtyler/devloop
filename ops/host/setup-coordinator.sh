#!/usr/bin/env bash
# Prepara l'host per il coordinatore di devloop (ADR 0002, precisazione del 2026-11-22; #530):
# l'utente, una copia di root di check-host.sh con i file che legge e i parametri, e un sudoers con
# soli comandi di lettura (ops/host/sudoers-coordinator).
# Idempotente: si può rieseguire, fa solo ciò che manca; va rieseguito dopo ogni aggiornamento di
# ops/, perché la copia resti uguale al checkout (check-host.sh avvisa se non lo è). Non scrive
# segreti. La CLI dell'agent del coordinatore, il suo login e il token di sola lettura per gh
# restano passi manuali (ops/README.md). Va eseguito dopo setup-devloop.sh.

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lib.sh
. "$OPS_DIR/lib.sh"

usage() {
    say "Uso: sudo bash $0 [--params FILE] [--dry-run] [--yes]"
    say
    say "$OPS_COMMON_HELP"
}

while [ $# -gt 0 ]; do
    case $1 in
        -h | --help) usage; exit 0 ;;
        *) ops_common_arg "$@" || exit 2; shift "$OPS_SHIFT" ;;
    esac
done

ops_load_params
ops_check_platform
require_root

# copy_file SRC DEST: DEST uguale a SRC byte per byte, 644 di root; lo riscrive solo se serve.
copy_file() {
    local src=$1 dest=$2
    [ -f "$src" ] || die "manca $src"
    if [ -f "$dest" ] && ! [ -L "$dest" ] && cmp -s "$src" "$dest" \
        && [ "$(stat -c '%a %U' "$dest")" = '644 root' ]; then
        ok "$dest è già aggiornato"
        return
    fi
    run install -m 644 -o root -g root "$src" "$dest" \
        || die "install non riesce a scrivere $dest: setup interrotto"
}

step "Utente $DEVLOOP_USER"
if user_exists "$DEVLOOP_USER" && [ -d "$DEVLOOP_HOME" ]; then
    ok "l'utente $DEVLOOP_USER e $DEVLOOP_HOME esistono"
elif [ "$DRY_RUN" = 1 ]; then
    warn "l'utente $DEVLOOP_USER o $DEVLOOP_HOME non esiste ancora: eseguire prima setup-devloop.sh"
else
    die "l'utente $DEVLOOP_USER o $DEVLOOP_HOME non esiste: eseguire prima setup-devloop.sh"
fi

step "Utente $COORDINATOR_USER"
if user_exists "$COORDINATOR_USER"; then
    ok "l'utente $COORDINATOR_USER esiste"
else
    run useradd --create-home --home-dir "$COORDINATOR_HOME" --shell /bin/bash "$COORDINATOR_USER"
fi

# Il coordinatore esegue come root solo questa copia, di root, mai il clone di devloop (che
# devloop scrive) né il checkout dell'operatore.
step "Copia di root di check-host.sh ($COORDINATOR_OPS_DIR)"
ensure_dir "$COORDINATOR_OPS_DIR" 755 root
ensure_dir "$COORDINATOR_OPS_DIR/host" 755 root
for name in $OPS_COORDINATOR_FILES; do
    copy_file "$OPS_DIR/$name" "$COORDINATOR_OPS_DIR/$name"
done
copy_file "$PARAMS_FILE" "$COORDINATOR_OPS_DIR/params.env"

step "sudoers del coordinatore ($COORDINATOR_SUDOERS_FILE)"
SUDOERS=$(render "$OPS_DIR/host/sudoers-coordinator")
if [ "$DRY_RUN" != 1 ]; then
    candidate=$(mktemp)
    printf '%s\n' "$SUDOERS" >"$candidate"
    if ! visudo -cf "$candidate" >/dev/null; then
        rm -f "$candidate"
        die "visudo rifiuta il sudoers generato: niente installato"
    fi
    rm -f "$candidate"
fi
put_file "$COORDINATOR_SUDOERS_FILE" 440 root "$SUDOERS" || true

step 'CLI e token (passi manuali)'
todo "installare la CLI dell'agent del coordinatore come $COORDINATOR_USER e fare il login"
todo "token fine-grained di sola lettura per gh, come $COORDINATOR_USER (ops/github/tokens.md)"

step 'Fatto'
say "Poi: ops/host/check-host.sh per verificare utenti, permessi, sudoers e la copia."
say "Il coordinatore verifica l'host con:"
say "  sudo /usr/bin/bash $COORDINATOR_OPS_DIR/host/check-host.sh --read-only"
