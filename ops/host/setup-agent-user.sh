#!/usr/bin/env bash
# Prepara l'utente degli agent (#415): l'utente di sistema con cui devloop avvia le CLI degli
# agent e i test, che non legge le chiavi delle app né lo state_dir. Crea l'utente, le sue
# directory delle CLI, le ACL (attraversamento della home di devloop; scrittura solo dentro i
# worktree e nelle directory che devloop crea per i turni e i test, mai in .git né nelle
# directory che le contengono; nessun accesso al resto dello state_dir; sessioni leggibili da
# devloop) e il sudoers che permette a devloop di avviare solo le CLI, bash e kill come
# quell'utente.
# Un solo utente esegue implementer e reviewer (#438): la sua home e le directory delle CLI
# (AGENT_CLI_DIRS di ops/lib.sh) diventano di root, del suo gruppo e con lo sticky bit (1770,
# #517), e i file che portano istruzioni, configurazione, plugin o binari (AGENT_CLI_SEALED,
# ~/.local compreso) di root e non scrivibili, con un segnaposto di root dove mancano: un turno
# non li cambia per i turni dopo. L'utente degli agent crea e sostituisce nelle directory delle
# CLI solo le proprie voci (credenziali, lock, sessioni, cache e log), e devloop ammette solo
# quelle della lista AGENT_CLI_ALLOWED: lo script segnala le altre. Crea sue le voci che le CLI
# creano al primo uso (AGENT_CLI_WRITABLE). --unseal rende all'utente degli agent le directory delle CLI, per installarle, aggiornarle
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
    step "Directory delle CLI di $AGENT_USER: di root, sticky (#517)"
    # Come la home: vi crea e sostituisce le proprie voci (credenziali salvate con un file
    # temporaneo e un rename, lock, cache), ma non rinomina né rimuove quelle di root, né le
    # directory stesse. Ciò che non deve creare (un file di istruzioni, un agent, una skill, un
    # hook) è un segnaposto di root, qui sotto; ciò che non è nella lista AGENT_CLI_ALLOWED
    # devloop lo rifiuta prima di ogni turno.
    for dir in "${AGENT_CLI_DIRS[@]}"; do
        owned "$AGENT_HOME/$dir" 1770 root "$AGENT_GROUP"
        # Con un'ACL (quella che una versione precedente dava a ~/.claude e ~/.codex per
        # $DEVLOOP_USER, quando erano 755) chmod cambia la maschera, non la voce del gruppo, che
        # resterebbe r-x: il gruppo dell'utente degli agent deve poter scrivere.
        ensure_acl "$AGENT_HOME/$dir" "g::rwx" "group::rwx"
    done
    step "~/.local di $AGENT_USER: di root (#438)"
    for dir in .local .local/bin; do
        owned "$AGENT_HOME/$dir" 755 root root
    done
fi

step "Voci che le CLI creano al primo uso: di $AGENT_USER (#494)"
# Le voci che una CLI crea al primo uso (AGENT_CLI_WRITABLE di ops/lib.sh, per agy la directory
# del wrapper del suo strumento di shell) le crea lo script, sue, anche se dal #517 l'utente
# degli agent potrebbe crearle da sé. devloop controlla ciò che vi scrive prima di ogni turno di
# agy.
for entry in "${AGENT_CLI_WRITABLE[@]}"; do
    if [ -L "$AGENT_HOME/$entry" ]; then
        todo "$AGENT_HOME/$entry è un link: sostituirlo con una directory di $AGENT_USER"
    else
        owned "$AGENT_HOME/$entry" 700 "$AGENT_USER" "$AGENT_GROUP"
    fi
done

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
    step "Istruzioni, configurazione e binari delle CLI: di root, leggibili, non scrivibili (#438)"
    # Del gruppo dell'utente degli agent, che li legge e attraversa (un settings.json 600 o una
    # directory 700 di una versione precedente restano leggibili) ma non li scrive né ne cambia
    # il modo: solo il proprietario, root, può.
    # unsealed PATH: qualcosa in PATH non è di root:AGENT_GROUP, o è scrivibile da gruppo o
    # altri, o non è leggibile (una directory attraversabile) dal gruppo. Senza il gruppo (in
    # --dry-run, prima di creare l'utente) tutto va sigillato.
    unsealed() {
        getent group "$AGENT_GROUP" >/dev/null || return 0
        [ -n "$(find "$1" \( ! -user root -o ! -group "$AGENT_GROUP" \
            -o \( ! -type l \( -perm /022 -o ! -perm -040 \) \) \
            -o \( -type d ! -perm -010 \) \) -print -quit 2>/dev/null)" ]
    }
    # placeholder ENTRY: il segnaposto di root della voce sigillata ENTRY che non esiste (#517),
    # perché nelle directory delle CLI, scrivibili, l'utente degli agent non possa crearla: un
    # file vuoto se il nome ha un'estensione ({} per un .json, che la CLI legge), altrimenti una
    # directory vuota. Di root e del gruppo dell'utente degli agent, che lo legge ma non lo
    # scrive, e non lo rinomina né lo rimuove (sticky bit).
    placeholder() {
        local path=$AGENT_HOME/$1 name=${1##*/} source=/dev/null
        case $name in
            ?*.*)
                case $name in *.json) source=$OPS_DIR/host/placeholder.json ;; esac
                run install -m 644 -o root -g "$AGENT_GROUP" "$source" "$path" ;;
            *) run install -d -m 755 -o root -g "$AGENT_GROUP" "$path" ;;
        esac
    }
    for entry in "${AGENT_CLI_SEALED[@]}"; do
        path="$AGENT_HOME/$entry"
        if ! [ -e "$path" ] && ! [ -L "$path" ]; then
            placeholder "$entry"
        elif unsealed "$path"; then
            run chown -hR "root:$AGENT_GROUP" "$path"
            # chmod segue un link dato come argomento: il target si controlla qui sotto.
            [ -L "$path" ] || run chmod -R g+rX,go-w "$path"
        else
            ok "$path: di root, leggibile da $AGENT_USER, non scrivibile"
        fi
    done
    # Un link che porta fuori da ciò che è sigillato (anche un eseguibile di ~/.local/bin, una
    # directory delle CLI o un file di istruzioni): il controllo all'avvio segue i link e
    # rifiuta un target che l'utente degli agent può cambiare, che va quindi reso di root a mano.
    home_real=$(readlink -f -- "$AGENT_HOME" 2>/dev/null || printf '%s' "$AGENT_HOME")
    sealed_target() {
        local entry
        for entry in "${AGENT_CLI_SEALED[@]}"; do
            case $1 in "$home_real/$entry" | "$home_real/$entry/"*) return 0 ;; esac
        done
        return 1
    }
    # Le directory delle CLI stesse (non ciò che contengono: sessioni e stato restano
    # dell'utente degli agent) e tutto ciò che è sigillato.
    for dir in "${AGENT_CLI_DIRS[@]}"; do
        [ -L "$AGENT_HOME/$dir" ] \
            && todo "$AGENT_HOME/$dir è un link: sostituirlo con una directory di root"
    done
    for entry in "${AGENT_CLI_SEALED[@]}"; do
        path="$AGENT_HOME/$entry"
        [ -e "$path" ] || [ -L "$path" ] || continue
        while IFS= read -r -d '' link; do
            target=$(readlink -f -- "$link") || target='(irrisolvibile)'
            sealed_target "$target" \
                || todo "$link porta a $target: renderlo di root, leggibile e non scrivibile da $AGENT_USER, o sostituire il link"
        done < <(find "$path" -type l -print0 2>/dev/null)
    done

    step "codex senza memorie: [features] memories = false in ~/.codex/config.toml (#517)"
    # Le memorie di codex (memories_1.sqlite) restano spente in modo esplicito, così un cambio
    # del default di codex non le riaccende: un turno non rilegge ciò che un altro vi lascia, e i
    # turni di codex girano in parallelo. devloop e check-host.sh rifiutano un config.toml senza
    # la riga, o che la imposta a un altro valore: lo script aggiunge la riga che manca, non ne
    # cambia una che c'è.
    CODEX_CONFIG="$AGENT_HOME/.codex/config.toml"
    if [ -L "$CODEX_CONFIG" ]; then
        todo "$CODEX_CONFIG è un link: impostare a mano [features] memories = false nel file a cui porta"
    else
        codex_new=$(mktemp)
        codex_status=0
        codex_why=$(codex_memories "$CODEX_CONFIG" "$codex_new") || codex_status=$?
        case $codex_status in
            0) ok "$CODEX_CONFIG imposta [features] memories = false" ;;
            3) codex_mode=$(stat -c %a "$CODEX_CONFIG" 2>/dev/null) || codex_mode=644
               if [ "$DRY_RUN" = 1 ]; then
                   say "  [dry-run] scriverebbe $CODEX_CONFIG (mode $codex_mode, proprietario root):"
                   sed 's/^/    | /' "$codex_new"
               else
                   run install -m "$codex_mode" -o root -g "$AGENT_GROUP" "$codex_new" "$CODEX_CONFIG"
               fi ;;
            *) todo "${codex_why:-$CODEX_CONFIG non si legge}: impostare a mano [features] memories = false; devloop non parte finché manca" ;;
        esac
        rm -f "$codex_new"
    fi

    step "Voci delle directory delle CLI: solo quelle della lista (#517)"
    # devloop rifiuta ogni turno finché una directory delle CLI contiene una voce che non è
    # nella lista (AGENT_CLI_ALLOWED), né sigillata né una directory delle CLI: lo script la
    # segnala, non la rimuove.
    unknown=0
    for dir in "${AGENT_CLI_DIRS[@]}"; do
        [ -d "$AGENT_HOME/$dir" ] && ! [ -L "$AGENT_HOME/$dir" ] || continue
        while IFS= read -r -d '' record && IFS= read -r -d '' target; do
            type=${record%%/*} name=${record#*/}
            if agent_cli_entry_known "$dir" "$name" "$type" "$target" "$AGENT_HOME"; then
                continue
            else
                known=$?
            fi
            unknown=1
            if [ "$known" = 4 ]; then
                todo "$AGENT_HOME/$dir/$name è un link a $target, che non è un file regolare (un altro link, che potrebbe uscire dalla directory, o nulla): rimuoverlo come $AGENT_USER, a loop fermo"
            elif [ "$known" = 3 ]; then
                todo "$AGENT_HOME/$dir/$name è un link a $target, fuori dalla directory che AGENT_CLI_LINKS ammette per quel nome: rimuoverlo come $AGENT_USER, a loop fermo"
            elif [ "$known" = 2 ]; then
                todo "$AGENT_HOME/$dir/$name è di tipo $type, che la lista AGENT_CLI_ALLOWED non ammette per quel nome: rimuoverla come $AGENT_USER, a loop fermo"
            else
                todo "$AGENT_HOME/$dir/$name non è nella lista AGENT_CLI_ALLOWED: rimuoverla come $AGENT_USER, a loop fermo, o aggiungerla alla lista con una PR (ops/host/migrate.md)"
            fi
        done < <(find "$AGENT_HOME/$dir" -mindepth 1 -maxdepth 1 -printf '%y/%f\0%l\0' 2>/dev/null)
    done
    [ "$unknown" = 1 ] || ok "le directory delle CLI contengono solo voci della lista"
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
