# Token da creare a mano

GitHub non ha un'API per creare i token fine-grained: si creano dall'interfaccia web, con
l'account dell'operatore, e non entrano mai nel repository.

## Token di sola lettura del supervisore

Serve al supervisore per le **letture** di `gh` (issue, PR, commenti, CI) dal suo utente di
sistema. Le sue **scritture** (commenti, review, label) non passano da questo token ma da una
GitHub App, con `scripts/agent-token.py`, che conia un token d'installazione con i soli permessi
dell'azione.

**Non** si usa un login admin né un token classico: un token così potrebbe scrivere, fare merge o
cambiare le impostazioni del repository.

Creazione: *Settings → Developer settings → Personal access tokens → Fine-grained tokens →
Generate new token*.

| Campo | Valore |
|---|---|
| Token name | per esempio `supervisor-<host>-readonly` |
| Expiration | una scadenza, per esempio 90 giorni; segnarla per rinnovarlo |
| Resource owner | il proprietario del repository (`REPO` in `params.env`) |
| Repository access | *Only select repositories*: solo il repository gestito |
| Repository permissions | `Metadata: Read-only`, `Contents: Read-only`, `Issues: Read-only`, `Pull requests: Read-only`, `Actions: Read-only` |
| Account permissions | nessuno |

Nient'altro: nessun permesso in scrittura, nessun altro repository.

Installazione, come utente del supervisore, senza che il token finisca nella cronologia della
shell o in un file del repository:

```bash
sudo -iu supervisor
gh auth login --hostname github.com --with-token   # incollare il token, poi Ctrl-D
gh auth status                                      # il login con il token
gh issue list --repo <owner>/<nome> --limit 1       # una lettura riesce
```

I permessi del token si controllano nella sua pagina su GitHub: `gh api repos/<owner>/<nome>`
mostra in `permissions` quelli dell'account, non quelli del token.

`gh` lo conserva nella propria configurazione dell'utente del supervisore (`~/.config/gh`).

**Un altro progetto sullo stesso host** (#571, #629). Supervisore e coordinatore sono del
progetto, con un utente per ruolo e per progetto (`supervisor-<progetto>`,
`coordinator-<progetto>`; [ops/README.md](../README.md#più-progetti-sullo-stesso-host)): ognuno ha
il **suo** token, generato come sopra con il solo repository del suo progetto e conservato nella
configurazione di `gh` del suo utente. Un token non si estende al repository di un altro progetto,
né si copia da un utente all'altro. Lo stesso vale per il token di sola lettura del coordinatore,
se il progetto lo ha.

**Rinnovo:** prima della scadenza si genera un token nuovo con gli stessi permessi (*Regenerate
token*) e si ripete `gh auth login --with-token`. **Revoca:** dalla stessa pagina, *Delete*, se il
token è finito altrove o se l'host viene dismesso.

## Nessun token per devloop

L'utente `devloop` non ha un login `gh` né un token personale: devloop agisce con le sue GitHub
App, con token d'installazione coniati a ogni azione dalle chiavi in `key_dir`
([docs/devloop/reference.md](../../docs/devloop/reference.md#github-identities)). Senza
l'app `merger` i merge delle modalità `strict` e `auto` userebbero un login `gh` locale: con le
app configurate non serve.
