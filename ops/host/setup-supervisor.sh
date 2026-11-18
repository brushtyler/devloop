#!/usr/bin/env bash
# Prepara l'host per il supervisore di devloop: utente, ACL di solo attraversamento sulla home
# di devloop (per leggere i log), sudoers minimale, clone, key_dir 700, impostazioni di Claude
# Code senza aggiornamenti automatici.
# Idempotente: si può rieseguire, fa solo ciò che manca. Non scrive segreti. Claude Code, il suo
# login, la chiave dell'app e il token di sola lettura per gh restano passi manuali (ops/README.md).
# Va eseguito dopo setup-devloop.sh.

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

PACKAGES='python3 git gh openssl acl nodejs npm sudo'

step 'Pacchetti di sistema'
missing=()
for pkg in $PACKAGES; do
    if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'install ok installed'; then
        ok "$pkg installato"
    else
        missing+=("$pkg")
    fi
done
if [ ${#missing[@]} -gt 0 ]; then
    run apt-get update
    run apt-get install -y --no-install-recommends "${missing[@]}"
fi

step "Utente $DEVLOOP_USER"
if user_exists "$DEVLOOP_USER" && [ -d "$DEVLOOP_HOME" ]; then
    ok "l'utente $DEVLOOP_USER e $DEVLOOP_HOME esistono"
elif [ "$DRY_RUN" = 1 ]; then
    warn "l'utente $DEVLOOP_USER o $DEVLOOP_HOME non esiste ancora: eseguire prima setup-devloop.sh"
else
    die "l'utente $DEVLOOP_USER o $DEVLOOP_HOME non esiste: eseguire prima setup-devloop.sh"
fi

step "Utente $SUPERVISOR_USER"
if user_exists "$SUPERVISOR_USER"; then
    ok "l'utente $SUPERVISOR_USER esiste"
else
    run useradd --create-home --home-dir "$SUPERVISOR_HOME" --shell /bin/bash "$SUPERVISOR_USER"
fi

step "Accesso ai log: ACL u:$SUPERVISOR_USER:--x su $DEVLOOP_HOME"
# Solo attraversamento: il supervisore raggiunge i log per percorso, senza poter elencare la
# home di devloop. ~/.ghapps, ~/.claude e ~/.codex di devloop restano 700.
acl_entry=''
if [ -d "$DEVLOOP_HOME" ]; then
    acl_entry=$(getfacl -p --omit-header "$DEVLOOP_HOME" 2>/dev/null | grep "^user:$SUPERVISOR_USER:" || true)
fi
if [ "$acl_entry" = "user:$SUPERVISOR_USER:--x" ]; then
    ok "ACL presente: $acl_entry"
else
    [ -z "$acl_entry" ] || warn "ACL attuale $acl_entry: la riduco a --x"
    run setfacl -m "u:$SUPERVISOR_USER:--x" "$DEVLOOP_HOME"
fi
for private in .ghapps .claude .codex; do
    dir="$DEVLOOP_HOME/$private"
    if [ -d "$dir" ] && [ "$(stat -c '%a' "$dir")" != 700 ]; then
        warn "$dir non è 700: rieseguire setup-devloop.sh"
    fi
done

step "sudoers del supervisore ($SUDOERS_FILE)"
SUDOERS=$(render "$OPS_DIR/host/sudoers-supervisor")
if [ "$DRY_RUN" != 1 ]; then
    candidate=$(mktemp)
    printf '%s\n' "$SUDOERS" >"$candidate"
    if ! visudo -cf "$candidate" >/dev/null; then
        rm -f "$candidate"
        die "visudo rifiuta il sudoers generato: niente installato"
    fi
    rm -f "$candidate"
fi
put_file "$SUDOERS_FILE" 440 root "$SUDOERS" || true

step "Clone di $REPO per $SUPERVISOR_USER"
if [ -d "$SUPERVISOR_CLONE/.git" ]; then
    ok "$SUPERVISOR_CLONE è già un clone"
else
    as_user "$SUPERVISOR_USER" git clone "https://github.com/$REPO.git" "$SUPERVISOR_CLONE"
fi

step 'Directory private (700)'
ensure_dir "$SUPERVISOR_KEY_DIR" 700 "$SUPERVISOR_USER"
ensure_dir "$SUPERVISOR_HOME/.claude" 700 "$SUPERVISOR_USER"
ensure_dir "$SUPERVISOR_HOME/.local" 755 "$SUPERVISOR_USER"
ensure_dir "$SUPERVISOR_HOME/.local/bin" 755 "$SUPERVISOR_USER"

step 'Claude Code senza aggiornamenti automatici'
SETTINGS="$SUPERVISOR_HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
    ok "$SETTINGS esiste: verificare a mano autoUpdates: false e DISABLE_AUTOUPDATER=1"
else
    put_file "$SETTINGS" 600 "$SUPERVISOR_USER" '{
  "autoUpdates": false,
  "env": {
    "DISABLE_AUTOUPDATER": "1"
  }
}' || true
fi
if [ -x "$SUPERVISOR_HOME/.local/bin/claude" ]; then
    ok "$SUPERVISOR_HOME/.local/bin/claude presente"
else
    todo "installare Claude Code come $SUPERVISOR_USER in $SUPERVISOR_HOME/.local/bin/claude e fare il login"
fi

step 'Chiavi e token (passi manuali)'
for role in $SUPERVISOR_KEYS; do
    if compgen -G "$SUPERVISOR_KEY_DIR/$APP_PREFIX$role.*.private-key.pem" >/dev/null 2>&1; then
        ok "chiave di $APP_PREFIX$role presente"
    else
        todo "copiare la chiave di $APP_PREFIX$role in $SUPERVISOR_KEY_DIR (mode 600, proprietario $SUPERVISOR_USER)"
    fi
done
todo "token fine-grained di sola lettura per gh, come $SUPERVISOR_USER (ops/github/tokens.md)"

step 'Fatto'
say "Poi: ops/host/check-host.sh per verificare utenti, permessi, ACL, sudoers e servizio."
