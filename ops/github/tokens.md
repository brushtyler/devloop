# Token

Nessun ruolo di devloop ha un token personale di GitHub da creare a mano: devloop, il supervisore
e il coordinatore agiscono con le loro GitHub App, con token d'installazione coniati per ogni
azione o comando dalle chiavi nelle loro `key_dir`, con i soli permessi che servono.

## Supervisore e coordinatore

Dal #643 (scelta S2 dell'operatore) il supervisore **legge** GitHub con la sua app, come il
coordinatore: `scripts/supervisor-token.py` con i soli permessi di lettura che servono
(`--perm issues=read`, `--perm pull_requests=read`, e `--perm actions=read`, `checks=read` o
`statuses=read` per la CI), e senza `--perm` per le scritture (commenti, review, label). Il
coordinatore fa lo stesso con `scripts/coordinator-token.py`
([runbook del supervisore](../../docs/devloop/roles/supervisor.md#github),
[runbook del coordinatore](../../docs/devloop/roles/coordinator.md#github)).

Nessuno dei due ha un login `gh` né un token fine-grained: nessun passo sul sito per ogni utente
di ruolo, nessuna scadenza da rinnovare. La chiave dell'app è l'unica credenziale del ruolo, e
sta solo nella `key_dir` del suo utente (700, la chiave 600). Un host che aveva il token di sola
lettura del supervisore o del coordinatore lo toglie con
[migrate.md, "Letture del supervisore con la sua app"](../host/migrate.md#letture-del-supervisore-con-la-sua-app-643).

**Non** si usa un login admin né un token classico per un ruolo: un token così potrebbe
scrivere, fare merge o cambiare le impostazioni del repository.

## Nessun token per devloop

L'utente `devloop` non ha un login `gh` né un token personale: devloop agisce con le sue GitHub
App, con token d'installazione coniati a ogni azione dalle chiavi in `key_dir`
([docs/devloop/reference.md](../../docs/devloop/reference.md#github-identities)). Senza
l'app `merger` i merge delle modalità `strict` e `auto` userebbero un login `gh` locale: con le
app configurate non serve.
