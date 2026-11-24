#!/usr/bin/env bash
# Prepara l'host per il supervisore di devloop: utente, ACL di solo attraversamento sulla home
# di devloop (per leggere i log) e di sola lettura sull'overlay e sul file di progetto dello
# state_dir (#543), la casella fra supervisore e coordinatore (#543), sudoers minimale, la copia
# di root del risveglio role-wait.sh e il watchdog dei ruoli (#602), clone,
# key_dir 700, impostazioni di Claude Code senza aggiornamenti automatici; con SOURCE_REPO anche un
# clone della sorgente al tag SOURCE_REF, da cui lanciare scripts/supervisor-token.py (#571).
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

# L'overlay e il file di progetto dello state_dir (#543): scripts/supervisor-token.py legge repo e
# [apps.supervisor] dall'overlay, e il supervisore legge la configurazione del progetto. Sono
# dell'operatore, 600: il supervisore li legge con un'ACL di sola lettura, che si aggiunge se
# manca. Un file sostituito (install, cp, rsync) perde l'ACL: si riesegue questo script.
# Lo state_dir è 750 (#573): il supervisore lo attraversa, senza elencarlo, per raggiungere
# run.log e i due file.
step "Accesso allo state_dir: ACL u:$SUPERVISOR_USER:--x su $STATE_PATH"
ensure_traverse_acl "$SUPERVISOR_USER" "$STATE_PATH"
for file in "$STATE_PATH/config.local.toml" "$STATE_PATH/project.toml"; do
    step "Lettura di $(basename "$file"): ACL u:$SUPERVISOR_USER:r-- su $file"
    ensure_read_acl "$SUPERVISOR_USER" "$file"
done

step "Casella fra supervisore e coordinatore: $COORDINATION_DIR, gruppo $COORDINATION_GROUP (#543)"
ensure_coordination "$SUPERVISOR_USER"

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

# Il risveglio del supervisore (#602): una copia di root di role-wait.sh, con i file che legge e i
# parametri, fuori dal clone e dalle home, da cui il supervisore lo esegue come suo utente.
step "Copia di root di role-wait.sh ($SUPERVISOR_OPS_DIR)"
# shellcheck disable=SC2086  # nomi di file senza spazi
ops_root_copy "$SUPERVISOR_OPS_DIR" $OPS_SUPERVISOR_FILES

# Il watchdog dei ruoli (#602): le unità template, una volta per host, e l'istanza del progetto
# quando il progetto ha anche il coordinatore (l'overlay lo dichiara e l'utente c'è, #571).
step "Watchdog dei ruoli ($WATCHDOG_UNIT, #602)"
if user_exists "$COORDINATOR_USER" && overlay_declares_coordinator "$STATE_PATH/config.local.toml"; then
    ensure_role_watchdog 1
else
    ensure_role_watchdog 0
fi

step "Clone di $REPO per $SUPERVISOR_USER"
if [ -d "$SUPERVISOR_CLONE/.git" ]; then
    # Un clone per progetto (ADR 0006, R8): uno di un altro repository è di un altro progetto,
    # e non si condivide. L'origin si legge dal file, senza eseguire git nel clone d'altri.
    origin=$(clone_origin "$SUPERVISOR_CLONE")
    slug=$(origin_slug "$origin")
    [ "${slug,,}" = "${REPO,,}" ] \
        || die "$SUPERVISOR_CLONE è un clone di '${origin:-origine sconosciuta}', non di $REPO: è di un altro progetto, scegliere un altro SUPERVISOR_CLONE"
    ok "$SUPERVISOR_CLONE è già un clone di $REPO"
else
    as_user "$SUPERVISOR_USER" git clone "https://github.com/$REPO.git" "$SUPERVISOR_CLONE"
fi

# Il repository gestito di un progetto con la sorgente a parte non contiene devloop, quindi
# nemmeno scripts/supervisor-token.py (#571): il supervisore lo lancia da un clone della
# sorgente, al tag SOURCE_REF del progetto.
TOKEN_CLONE=$SUPERVISOR_CLONE
if [ -n "$SOURCE_REPO" ]; then
    step "Clone di $SOURCE_REPO al tag $SOURCE_REF per $SUPERVISOR_USER (i token della sua app)"
    ensure_source_clone "$SUPERVISOR_USER" "$SUPERVISOR_SOURCE_CLONE"
    TOKEN_CLONE=$SUPERVISOR_SOURCE_CLONE
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
# key_dir di [apps] nell'overlay è quella di devloop: il supervisore indica la propria (#571).
say "  i token della sua app si coniano come $SUPERVISOR_USER, dal clone $TOKEN_CLONE, con la sua key_dir e lo state_dir del progetto:"
say "  DEVLOOP_APP_KEY_DIR=$SUPERVISOR_KEY_DIR python3 scripts/supervisor-token.py --state-dir $STATE_PATH -- <comando>"
todo "token fine-grained di sola lettura per gh, come $SUPERVISOR_USER, con accesso a $REPO: il supervisore è del progetto, con il suo utente e il suo token (#629; ops/github/tokens.md)"

step 'Fatto'
say "Poi: ops/host/check-host.sh per verificare utenti, permessi, ACL, sudoers e servizio."
say "Il supervisore aspetta i suoi eventi con il risveglio dei ruoli (#602):"
say "  bash $SUPERVISOR_OPS_DIR/host/role-wait.sh --role supervisor"
