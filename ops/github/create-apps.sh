#!/usr/bin/env bash
# Genera una pagina HTML locale con un pulsante per ciascuna GitHub App: il pulsante invia a
# GitHub il manifest dell'app (ops/github/apps/<ruolo>.json, flusso "manifest"), con il nome
# <APP_PREFIX><ruolo> e gli URL del repository di params.env. GitHub mostra la pagina di
# creazione già compilata; l'operatore conferma, completa entro un'ora lo scambio del `code`
# (POST /app-manifests/<code>/conversions, che restituisce la chiave privata), poi installa l'app
# solo sul repository (ops/README.md).
#
# Le app sono quelle di APPS (params.env): per default dispatcher, merger, claude-agent,
# codex-agent, agy-agent, implementer, supervisor e coordinator. Quella del coordinatore (#549;
# ADR 0002, precisazione del 2026-11-22) ha issues e pull_requests in scrittura e actions, checks
# e statuses in sola lettura, per la diagnosi della CI, senza contents; la sua chiave va nella
# key_dir dell'utente coordinator (COORDINATOR_KEY_DIR), e il coordinatore ne conia i token con
# scripts/coordinator-token.py.
#
# Lo script non chiama GitHub, non legge e non scrive segreti: scrive solo la pagina HTML.
# Idempotente: rieseguito, riscrive la stessa pagina.

OPS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=../lib.sh
. "$OPS_DIR/lib.sh"

OUT="${TMPDIR:-/tmp}/create-apps.html"

usage() {
    say "Uso: bash $0 [--params FILE] [--dry-run] [--out FILE]"
    say
    say "  --out FILE      la pagina da scrivere (default: $OUT)"
    say "$OPS_COMMON_HELP"
}

while [ $# -gt 0 ]; do
    case $1 in
        -h | --help) usage; exit 0 ;;
        --out)
            [ $# -ge 2 ] || die '--out vuole un file'
            OUT=$2
            shift 2
            ;;
        *) ops_common_arg "$@" || exit 2; shift "$OPS_SHIFT" ;;
    esac
done

ops_load_params

step "GitHub App per $REPO (prefisso $APP_PREFIX)"
for role in $APPS; do
    [ -f "$OPS_DIR/github/apps/$role.json" ] || die "manca il manifest ops/github/apps/$role.json"
done

# La pagina, generata da python3 (JSON e HTML con l'escape giusto).
PAGE=$(python3 - "$OPS_DIR/github/apps" "$REPO" "$APP_PREFIX" "$GITHUB_OWNER_TYPE" $APPS <<'EOF'
import html
import json
import sys
from pathlib import Path

apps_dir, repo, prefix, owner_type, *roles = sys.argv[1:]
owner = repo.split('/', 1)[0]
if owner_type == 'org':
    action = f'https://github.com/organizations/{owner}/settings/apps/new'
else:
    action = 'https://github.com/settings/apps/new'
forms = []
for role in roles:
    text = Path(apps_dir, f'{role}.json').read_text().replace('@REPO@', repo)
    manifest = json.loads(text)
    if '@' in json.dumps(manifest):
        sys.exit(f'{role}.json: segnaposto non risolto')
    manifest['name'] = prefix + role
    permissions = ', '.join(f'{k}: {v}' for k, v in sorted(manifest['default_permissions'].items()))
    print(f'  {manifest["name"]}: {permissions}', file=sys.stderr)
    forms.append(
        f'<form action="{html.escape(action)}" method="post" target="_blank">\n'
        f'  <input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest))}">\n'
        f'  <p><button type="submit">Crea {html.escape(manifest["name"])}</button>\n'
        f'  <code>{html.escape(permissions)}</code></p>\n'
        f'</form>')
print(f'''<!doctype html>
<html lang="it">
<head><meta charset="utf-8"><title>GitHub App di devloop per {html.escape(repo)}</title></head>
<body>
<h1>GitHub App di devloop per {html.escape(repo)}</h1>
<p>Un pulsante per app. Dopo la creazione GitHub torna al repository con un parametro
<code>code</code> nell'indirizzo: entro un'ora completare lo scambio
<code>POST /app-manifests/&lt;code&gt;/conversions</code> come descritto in ops/README.md (la risposta
contiene la chiave privata: va in key_dir, mode 600, mai stampata). Poi <em>Install App</em>,
<em>Only select repositories</em>, solo {html.escape(repo)}.</p>
{chr(10).join(forms)}
</body>
</html>''')
EOF
)

if [ "$DRY_RUN" = 1 ]; then
    say "  [dry-run] scriverebbe $OUT ($(printf '%s\n' "$PAGE" | wc -l) righe), senza segreti"
else
    printf '%s\n' "$PAGE" >"$OUT"
    ok "scritta $OUT"
    say "  aprirla nel browser con la sessione GitHub del proprietario di $REPO"
fi
