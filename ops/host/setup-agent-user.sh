#!/usr/bin/env bash
# Prepara l'utente degli agent (#415): l'utente di sistema con cui devloop avvia le CLI degli
# agent e i test, che non legge le chiavi delle app né lo state_dir. Crea l'utente, le sue
# directory delle CLI, le ACL (attraversamento della home di devloop; scrittura solo dentro i
# worktree e nelle directory che devloop crea per i turni e i test, mai in .git né nelle
# directory che le contengono; nessun accesso al resto dello state_dir; sessioni leggibili da
# devloop) e il sudoers che permette a devloop di avviare solo le CLI, bash e kill come
# quell'utente.
# Un solo utente esegue implementer e reviewer (#438): la sua home e i file delle CLI che portano
# istruzioni, configurazione o binari (AGENT_CLI_DIRS, AGENT_CLI_SEALED di ops/lib.sh, ~/.local
# compreso) diventano di root, così un turno non li cambia per i turni dopo. L'utente degli agent
# scrive solo credenziali, sessioni, cache e log: le voci che le CLI vi hanno già creato restano
# sue. --unseal rende all'utente degli agent le directory delle CLI, per installarle, aggiornarle
# o fare un login a loop fermo; devloop non parte finché lo script non viene rieseguito senza.
# Idempotente: si può rieseguire, fa solo ciò che manca. Non scrive segreti. Le CLI, i loro
# login e la chiave [agent_user] nell'overlay restano passi manuali (ops/README.md).
# Va eseguito dopo setup-devloop.sh, e di nuovo dopo aver copiato file nello state_dir.

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lib.sh
. "$OPS_DIR/lib.sh"

UNSEAL=0

usage() {
    say "Uso: sudo bash $0 [--params FILE] [--dry-run] [--yes] [--unseal]"
    say
    say "  --unseal        rende all'utente degli agent le directory delle sue CLI, per installarle,"
    say "                  aggiornarle o fare un login a loop fermo; poi si riesegue senza"
    say "$OPS_COMMON_HELP"
}

while [ $# -gt 0 ]; do
    case $1 in
        -h | --help) usage; exit 0 ;;
        --unseal) UNSEAL=1; shift ;;
        *) ops_common_arg "$@" || exit 2; shift "$OPS_SHIFT" ;;
    esac
done

ops_load_params
ops_check_platform
require_root

STATE="$STATE_PATH"

# acl_entry PATH ENTRY: la riga ENTRY (per esempio user:x:--x o default:user:x:rwx) nell'ACL
# di PATH, o niente. getfacl può aggiungere alla riga un commento #effective.
acl_entry() {
    getfacl -p --omit-header "$1" 2>/dev/null | grep -E -- "^$2([[:space:]]|\$)" || true
}

# ensure_acl PATH SPEC ENTRY...: applica `setfacl -m SPEC` a PATH se manca una delle righe ENTRY.
ensure_acl() {
    local path=$1 spec=$2 entry missing=0
    shift 2
    for entry in "$@"; do
        [ -n "$(acl_entry "$path" "$entry")" ] || missing=1
    done
    if [ "$missing" = 0 ]; then
        ok "ACL di $path: $*"
    else
        run setfacl -m "$spec" "$path"
    fi
}

step "Utente $DEVLOOP_USER"
if user_exists "$DEVLOOP_USER" && [ -d "$DEVLOOP_HOME" ]; then
    ok "l'utente $DEVLOOP_USER e $DEVLOOP_HOME esistono"
elif [ "$DRY_RUN" = 1 ]; then
    warn "l'utente $DEVLOOP_USER o $DEVLOOP_HOME non esiste ancora: eseguire prima setup-devloop.sh"
else
    die "l'utente $DEVLOOP_USER o $DEVLOOP_HOME non esiste: eseguire prima setup-devloop.sh"
fi

step "Utente $AGENT_USER"
if user_exists "$AGENT_USER"; then
    ok "l'utente $AGENT_USER esiste"
else
    run useradd --create-home --home-dir "$AGENT_HOME" --shell /bin/bash "$AGENT_USER"
fi

AGENT_GROUP=$(id -gn "$AGENT_USER" 2>/dev/null || printf '%s' "$AGENT_USER")

# owned PATH MODE OWNER GROUP: la directory PATH con MODE, di OWNER:GROUP; la crea se manca.
owned() {
    local path=$1 mode=$2 owner=$3 group=$4
    if [ ! -d "$path" ]; then
        run install -d -m "$mode" -o "$owner" -g "$group" "$path"
    elif [ "$(stat -c '%a %U %G' "$path")" = "$mode $owner $group" ]; then
        ok "$path: $mode, $owner:$group"
    else
        run chown "$owner:$group" "$path"
        run chmod "$mode" "$path"
    fi
}

step "Home di $AGENT_USER: di root, sticky (#438)"
# L'utente degli agent vi crea le voci di stato e di cache delle CLI, ma non rinomina né rimuove
# quelle di root: le directory delle CLI e ~/.local.
owned "$AGENT_HOME" 1770 root "$AGENT_GROUP"

if [ "$UNSEAL" = 1 ]; then
    step "Directory delle CLI aperte a $AGENT_USER (--unseal)"
    for entry in "${AGENT_CLI_DIRS[@]}" .local; do
        if [ -e "$AGENT_HOME/$entry" ]; then
            run chown -hR "$AGENT_USER:$AGENT_GROUP" "$AGENT_HOME/$entry"
        else
            run install -d -m 755 -o "$AGENT_USER" -g "$AGENT_GROUP" "$AGENT_HOME/$entry"
        fi
    done
    [ -d "$AGENT_HOME/.local/bin" ] \
        || run install -d -m 755 -o "$AGENT_USER" -g "$AGENT_GROUP" "$AGENT_HOME/.local/bin"
    warn "$AGENT_USER può ora cambiare istruzioni, configurazione e binari delle CLI: devloop non parte finché non si riesegue lo script senza --unseal"
else
    step "Directory delle CLI di $AGENT_USER: di root (#438)"
    # Vi entra, ma non vi crea nulla (un file di istruzioni, un agent, una skill, un hook) e non
    # le rinomina.
    for dir in "${AGENT_CLI_DIRS[@]}" .local .local/bin; do
        owned "$AGENT_HOME/$dir" 755 root root
    done
fi

step "Sessioni di $AGENT_USER"
# ~/.claude/projects e ~/.codex/sessions: le sessioni restano dell'utente degli agent, come i
# login e le altre voci che le CLI creano; devloop le legge (stallo, devloop follow).
for dir in "$AGENT_HOME/.claude/projects" "$AGENT_HOME/.codex/sessions"; do
    if [ -d "$dir" ]; then
        ok "$dir esiste"
    else
        run install -d -m 750 -o "$AGENT_USER" -g "$AGENT_GROUP" "$dir"
    fi
done

step 'Claude Code senza aggiornamenti automatici'
SETTINGS="$AGENT_HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
    ok "$SETTINGS esiste: verificare a mano autoUpdates: false e DISABLE_AUTOUPDATER=1"
else
    put_file "$SETTINGS" 644 root '{
  "autoUpdates": false,
  "env": {
    "DISABLE_AUTOUPDATER": "1"
  }
}' || true
fi

if [ "$UNSEAL" != 1 ]; then
    step "Istruzioni, configurazione e binari delle CLI: di root, non scrivibili (#438)"
    for entry in "${AGENT_CLI_SEALED[@]}"; do
        path="$AGENT_HOME/$entry"
        if ! [ -e "$path" ] && ! [ -L "$path" ]; then
            ok "$path non esiste: $AGENT_USER non può crearlo"
        elif [ -n "$(find "$path" \( ! -user root -o ! -group root -o \( ! -type l -perm /022 \) \) \
                -print -quit 2>/dev/null)" ]; then
            run chown -hR root:root "$path"
            run chmod -R go-w "$path"
        else
            ok "$path: di root, non scrivibile"
        fi
    done
    # Un eseguibile fuori da ~/.local: la directory del file a cui porta va resa di root a mano.
    for cli in "${AGENT_CLIS[@]}"; do
        exe="$AGENT_HOME/.local/bin/$cli"
        [ -e "$exe" ] || continue
        target=$(readlink -f -- "$exe")
        case $target in
            "$AGENT_HOME/.local/"* | "$AGENT_HOME/.codex/packages/"*) ;;
            *) todo "$exe porta a $target: renderne la directory di root e non scrivibile da $AGENT_USER" ;;
        esac
    done
fi

step "Sessioni di $AGENT_USER leggibili da $DEVLOOP_USER"
ensure_acl "$AGENT_HOME" "u:$DEVLOOP_USER:--x" "user:$DEVLOOP_USER:--x"
for dir in "$AGENT_HOME/.claude" "$AGENT_HOME/.codex"; do
    ensure_acl "$dir" "u:$DEVLOOP_USER:--x" "user:$DEVLOOP_USER:--x"
done
for dir in "$AGENT_HOME/.claude/projects" "$AGENT_HOME/.codex/sessions"; do
    ensure_acl "$dir" "u:$DEVLOOP_USER:r-x,d:u:$DEVLOOP_USER:r-x,d:u:$AGENT_USER:rwx" \
        "user:$DEVLOOP_USER:r-x" "default:user:$DEVLOOP_USER:r-x"
done

step "Accesso di $AGENT_USER al clone di devloop"
# Solo attraversamento della home di devloop: ~/.ghapps, ~/.claude e ~/.codex restano 700.
ensure_acl "$DEVLOOP_HOME" "u:$AGENT_USER:--x" "user:$AGENT_USER:--x"
# I metadati git del clone (.git, .git/worktrees/<nome> compresi) restano solo leggibili: chi li
# scrive può far eseguire a devloop un hook o una configurazione sua quando committa in un
# worktree. Le ACL che una versione precedente dava qui all'utente degli agent si tolgono.
GIT_WORKTREES="$DEVLOOP_CLONE/.git/worktrees"
if [ -d "$GIT_WORKTREES" ] \
    && getfacl -R -p "$GIT_WORKTREES" 2>/dev/null | grep -qE "^(default:)?user:$AGENT_USER:"; then
    run setfacl -R -b "$GIT_WORKTREES"
else
    ok "nessuna ACL di $AGENT_USER in $GIT_WORKTREES"
fi

step "state_dir: dentro i worktree e le directory dei turni sì, il resto no"
for dir in worktrees agent-shared agent-shared/schemas agent-shared/turns agent-shared/tmp \
    agent-shared/gh-config; do
    [ -d "$STATE/$dir" ] || as_user "$DEVLOOP_USER" mkdir -p "$STATE/$dir"
done
# Lo state_dir: solo attraversamento, e nessun accesso alle voci che vi si creeranno.
ensure_acl "$STATE" "u:$AGENT_USER:--x,d:u:$AGENT_USER:---" \
    "user:$AGENT_USER:--x" "default:user:$AGENT_USER:---"
# acl_container DIR: l'utente degli agent entra in DIR ma non lo scrive, così non sostituisce
# né collega ciò che devloop vi crea; scrive in ciò che vi si creerà (un worktree, la directory
# di un turno o dei test), che devloop può poi rimuovere.
acl_container() {
    ensure_acl "$1" "u:$AGENT_USER:r-x,d:u:$AGENT_USER:rwx,d:u:$DEVLOOP_USER:rwx" \
        "user:$AGENT_USER:r-x" "default:user:$AGENT_USER:rwx" "default:user:$DEVLOOP_USER:rwx"
}
acl_container "$STATE/worktrees"
# La directory condivisa e gli schemi: solo lettura, anche ciò che vi si creerà.
for dir in agent-shared agent-shared/schemas; do
    ensure_acl "$STATE/$dir" "u:$AGENT_USER:r-x,d:u:$AGENT_USER:r-x" \
        "user:$AGENT_USER:r-x" "default:user:$AGENT_USER:r-x"
done
acl_container "$STATE/agent-shared/turns"
acl_container "$STATE/agent-shared/tmp"
# La configurazione vuota di gh: devloop non la legge né la scrive.
ensure_acl "$STATE/agent-shared/gh-config" \
    "u:$AGENT_USER:rwx,d:u:$AGENT_USER:rwx,d:u:$DEVLOOP_USER:rwx" \
    "user:$AGENT_USER:rwx" "default:user:$AGENT_USER:rwx" "default:user:$DEVLOOP_USER:rwx"
# Le voci già presenti (journal, log, overlay, copie fissate, …): nessun accesso.
if [ -d "$STATE" ]; then
    for entry in "$STATE"/* "$STATE"/.[!.]*; do
        [ -e "$entry" ] || continue
        case $(basename "$entry") in worktrees | agent-shared) continue ;; esac
        [ -L "$entry" ] && continue  # current: la copia a cui punta ha la sua voce
        ensure_acl "$entry" "u:$AGENT_USER:---" "user:$AGENT_USER:---"
    done
fi

step "sudoers dell'utente degli agent ($AGENT_SUDOERS_FILE)"
SUDOERS=$(render "$OPS_DIR/host/sudoers-agent")
if [ "$DRY_RUN" != 1 ]; then
    candidate=$(mktemp)
    printf '%s\n' "$SUDOERS" >"$candidate"
    if ! visudo -cf "$candidate" >/dev/null; then
        rm -f "$candidate"
        die "visudo rifiuta il sudoers generato: niente installato"
    fi
    rm -f "$candidate"
fi
put_file "$AGENT_SUDOERS_FILE" 440 root "$SUDOERS" || true

step 'CLI degli agent (passi manuali, ops/README.md)'
for cli in "${AGENT_CLIS[@]}"; do
    if [ -x "$AGENT_HOME/.local/bin/$cli" ]; then
        ok "$AGENT_HOME/.local/bin/$cli presente"
    else
        todo "con --unseal, installare $cli come $AGENT_USER, con l'eseguibile in $AGENT_HOME/.local/bin/$cli, e fare il login; poi rieseguire senza --unseal"
    fi
done

step 'Overlay di devloop'
OVERLAY="$STATE/config.local.toml"
if [ -f "$OVERLAY" ] && python3 - "$OVERLAY" "$AGENT_USER" <<'EOF'
import sys, tomllib
data = tomllib.load(open(sys.argv[1], 'rb'))
sys.exit(0 if data.get('agent_user', {}).get('name') == sys.argv[2] else 1)
EOF
then
    ok "$OVERLAY imposta [agent_user] name = \"$AGENT_USER\""
else
    todo "impostare [agent_user] name = \"$AGENT_USER\" in $OVERLAY, poi riavviare devloop"
fi

step 'Fatto'
if [ "$UNSEAL" = 1 ]; then
    say "Poi: CLI e login come $AGENT_USER, e di nuovo questo script senza --unseal prima di avviare devloop (ops/README.md)."
else
    say "Poi: CLI e login come $AGENT_USER (con --unseal), l'overlay, ops/host/check-host.sh, riavvio del servizio (ops/README.md)."
fi
