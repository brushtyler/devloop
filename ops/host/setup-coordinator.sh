#!/usr/bin/env bash
# Prepara l'host per il coordinatore di devloop (ADR 0002, precisazione del 2026-11-22; #530):
# l'utente, l'ACL di solo attraversamento sulla home di devloop e quella di sola lettura
# sull'overlay (#554), per leggere l'overlay, la key_dir
# della sua GitHub App (700, #549), la casella fra supervisore e coordinatore (#543), una copia di
# root di check-host.sh e della sveglia coordinator-wait.sh (#543) con i file che leggono e i
# parametri, e un sudoers con soli comandi di lettura e la sveglia (ops/host/sudoers-coordinator);
# con SOURCE_REPO anche un clone della sorgente al tag SOURCE_REF, da cui lanciare
# scripts/coordinator-token.py (#571).
# Idempotente: si può rieseguire, fa solo ciò che manca; va rieseguito dopo ogni aggiornamento di
# ops/, perché la copia resti uguale al checkout (check-host.sh avvisa se non lo è). Non scrive
# segreti. La chiave dell'app, la CLI dell'agent del coordinatore, il suo login e il token di sola
# lettura per gh restano passi manuali (ops/README.md). Va eseguito dopo setup-devloop.sh.

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

# Prima di creare utente, copia o sudoers: ogni directory sopra COORDINATOR_OPS_DIR che esiste già
# è di root e non scrivibile dal gruppo né dagli altri (nemmeno con lo sticky bit), e nessuna,
# né la copia né la sua host/, è un link. Altrimenti chi può scrivere in una di esse rinomina la
# copia e ne mette un'altra al suo posto, che il sudoers gli fa eseguire come root. Le directory
# che mancano le crea root (install -d, umask 022). check-host.sh rifà lo stesso controllo.
check_ops_dir_ancestors() {
    local q owner mode
    for q in "$COORDINATOR_OPS_DIR/host" "$COORDINATOR_OPS_DIR"; do
        if [ -L "$q" ]; then
            die "$q è un link: la copia non può esserlo; niente installato"
        fi
    done
    q=$COORDINATOR_OPS_DIR
    while [ "$q" != / ]; do
        q=$(dirname "$q")
        if [ -L "$q" ]; then
            die "$q è un link: COORDINATOR_OPS_DIR deve stare sotto directory di root, non link; niente installato"
        fi
        if ! [ -e "$q" ]; then
            continue
        fi
        if ! [ -d "$q" ]; then
            die "$q non è una directory: niente installato"
        fi
        read -r owner mode < <(stat -c '%U %a' "$q")
        if [ "$owner" != root ] || (( 8#$mode & 8#022 )); then
            die "$q è $mode $owner: COORDINATOR_OPS_DIR deve stare sotto directory di root non scrivibili dal gruppo né dagli altri; niente installato"
        fi
    done
    ok "le directory sopra $COORDINATOR_OPS_DIR sono di root, non scrivibili da altri"
}
umask 022
step "Directory sopra $COORDINATOR_OPS_DIR"
check_ops_dir_ancestors

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

# L'overlay di devloop (#549): scripts/coordinator-token.py ne legge repo e [apps.coordinator],
# come utente coordinator. Con lo state_dir nella home di devloop (che useradd crea 750) serve
# l'attraversamento, e solo quello, come per il supervisore: il coordinatore raggiunge l'overlay
# per percorso, senza elencare la home; key_dir, ~/.claude e ~/.codex di devloop restano 700.
step "Accesso all'overlay: ACL u:$COORDINATOR_USER:--x su $DEVLOOP_HOME"
acl_entry=''
if [ -d "$DEVLOOP_HOME" ]; then
    acl_entry=$(getfacl -p --omit-header "$DEVLOOP_HOME" 2>/dev/null | grep "^user:$COORDINATOR_USER:" || true)
fi
if [ "$acl_entry" = "user:$COORDINATOR_USER:--x" ]; then
    ok "ACL presente: $acl_entry"
else
    [ -z "$acl_entry" ] || warn "ACL attuale $acl_entry: la riduco a --x"
    run setfacl -m "u:$COORDINATOR_USER:--x" "$DEVLOOP_HOME"
fi
for private in "$DEVLOOP_KEY_DIR" "$DEVLOOP_HOME/.claude" "$DEVLOOP_HOME/.codex"; do
    if [ -d "$private" ] && [ "$(stat -c '%a' "$private")" != 700 ]; then
        warn "$private non è 700: rieseguire setup-devloop.sh"
    fi
done

# L'overlay stesso (#554): l'operatore lo tiene 600, quindi il coordinatore lo legge solo con
# un'ACL di sola lettura, che si aggiunge se manca. Il file di progetto (project.toml) no:
# coordinator-token.py non lo legge. Un overlay sostituito (install, cp, rsync) perde l'ACL: si
# riesegue questo script.
OVERLAY="$STATE_PATH/config.local.toml"
step "Lettura dell'overlay: ACL u:$COORDINATOR_USER:r-- su $OVERLAY"
ensure_read_acl "$COORDINATOR_USER" "$OVERLAY"

# La casella fra supervisore e coordinatore (#543), che anche setup-supervisor.sh prepara: il
# coordinatore vi entra con il gruppo, e la sua sveglia (coordinator-wait.sh) la legge.
step "Casella fra supervisore e coordinatore: $COORDINATION_DIR, gruppo $COORDINATION_GROUP (#543)"
ensure_coordination "$COORDINATOR_USER"

# La key_dir della sua app (#549): solo il coordinatore ne legge la chiave, con
# scripts/coordinator-token.py; devloop non la legge mai.
step "key_dir del coordinatore (700)"
ensure_dir "$COORDINATOR_KEY_DIR" 700 "$COORDINATOR_USER"

# Il clone da cui il coordinatore lancia scripts/coordinator-token.py (#565): con la sorgente a
# parte il repository gestito non contiene devloop, quindi un clone della sorgente, al tag
# SOURCE_REF del progetto, che questo script prepara (#571).
TOKEN_CLONE=$COORDINATOR_HOME/${REPO#*/}
if [ -n "$SOURCE_REPO" ]; then
    step "Clone di $SOURCE_REPO al tag $SOURCE_REF per $COORDINATOR_USER (i token della sua app)"
    ensure_source_clone "$COORDINATOR_USER" "$COORDINATOR_SOURCE_CLONE"
    TOKEN_CLONE=$COORDINATOR_SOURCE_CLONE
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

step 'Chiave, CLI e token (passi manuali)'
for role in $COORDINATOR_KEYS; do
    if compgen -G "$COORDINATOR_KEY_DIR/$APP_PREFIX$role.*.private-key.pem" >/dev/null 2>&1; then
        ok "chiave di $APP_PREFIX$role presente"
    else
        todo "copiare la chiave di $APP_PREFIX$role in $COORDINATOR_KEY_DIR (mode 600, proprietario $COORDINATOR_USER)"
    fi
done
# key_dir di [apps] nell'overlay è quella di devloop: il coordinatore indica la propria (#549).
todo "coniare i token come $COORDINATOR_USER, dal suo clone ($TOKEN_CLONE), con la sua key_dir e lo state_dir di devloop:"
say "  DEVLOOP_APP_KEY_DIR=$COORDINATOR_KEY_DIR python3 scripts/coordinator-token.py --state-dir $STATE_PATH -- <comando>"
todo "installare la CLI dell'agent del coordinatore come $COORDINATOR_USER e fare il login"
todo "token fine-grained di sola lettura per gh, come $COORDINATOR_USER, con accesso a $REPO (ops/github/tokens.md)"
todo "app coordinator del progetto in [apps.coordinator] dell'overlay $STATE_PATH/config.local.toml: senza, check-host.sh non verifica il coordinatore di questo progetto"

step 'Fatto'
say "Poi: ops/host/check-host.sh per verificare utenti, permessi, sudoers e la copia."
say "Il coordinatore verifica l'host con:"
say "  sudo /usr/bin/bash $COORDINATOR_OPS_DIR/host/check-host.sh --read-only"
say "e aspetta i suoi eventi con la sveglia (#543):"
say "  bash $COORDINATOR_OPS_DIR/host/coordinator-wait.sh"
