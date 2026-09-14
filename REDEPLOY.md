# Redeploy

Pushing local changes to the live Container App. For the *first* deployment on a
fresh subscription, see `DEPLOY.md` instead.

| | |
|---|---|
| subscription | `avo-internal-sandbox` |
| resource group | `rg-ntnu-ws-2026` |
| container app | `avo-lab` |
| registry | `caec7709e914acr` |
| live URL | https://avo-lab.lemonfield-d248f0b0.swedencentral.azurecontainerapps.io |
| currently deployed | `v6` (`v7` is built and waiting in the registry) |

---

## The one rule

**A redeploy restarts the container, and all event state lives in memory.** Teams,
scores, breaches, brackets — gone. `snapshot.json` sits on ephemeral disk, so it
survives a crash but not a new revision.

Never redeploy during the workshop. Do it the day before, or between sessions.

---

## Redeploy

Two commands. Run them from the repo root. Bump the tag each time so the tag
says what is live at a glance.

Build the image in Azure (no local Docker needed, takes 2–4 minutes):

```bash
$env:PYTHONUTF8='1'; az acr build --registry caec7709e914acr --image avo-lab:v7 --file Dockerfile .
```

The `PYTHONUTF8` prefix matters on Windows: the build log contains non-ASCII
characters (the Norwegian deck), and without it the `az` CLI can crash with
`UnicodeEncodeError` while *printing* the log even though the build itself
succeeded. If that ever happens, check whether the tag landed before rebuilding:

```bash
az acr repository show-tags --name caec7709e914acr --repository avo-lab --orderby time_desc -o tsv
```

Point the app at it:

```bash
az containerapp update --name avo-lab --resource-group rg-ntnu-ws-2026 --image caec7709e914acr.azurecr.io/avo-lab:v7
```

This touches **only** the image. Env vars, secrets and the single-replica pin are
left alone, which is why it is safer than re-running `az containerapp up`.

## Confirm it took

```bash
az containerapp logs show --name avo-lab --resource-group rg-ntnu-ws-2026 --tail 30
```

You want the startup banner reporting `mode bifrost (azure/gpt-5.4-mini)`, your real join
code, and no `ADMIN_CODE is still the default` warning.

```bash
az containerapp show --name avo-lab --resource-group rg-ntnu-ws-2026 --query "{image:properties.template.containers[0].image, min:properties.template.scale.minReplicas, max:properties.template.scale.maxReplicas}" -o table
```

`min` and `max` must both be **1**. Two replicas means two independent
tournaments and a round-robin that silently splits in half.

---

## Rollback

**Roll back by image tag, not by revision.** This app runs in single-revision
mode, so an update *replaces* the revision rather than keeping the old one
alongside — there is normally nothing to reactivate. The images stay in the
registry, though, so re-pointing at an earlier tag always works.

See what you can go back to:

```bash
az acr repository show-tags --name caec7709e914acr --repository avo-lab --orderby time_desc -o tsv
```

Then point the app at one:

```bash
az containerapp update --name avo-lab --resource-group rg-ntnu-ws-2026 --image caec7709e914acr.azurecr.io/avo-lab:gatekeeper-v2
```

To check which image is live and how many revisions exist:

```bash
az containerapp revision list --name avo-lab --resource-group rg-ntnu-ws-2026 --query "[].{name:name, image:properties.template.containers[0].image, active:properties.active, created:properties.createdTime}" -o table
```

If that ever lists more than one, `az containerapp revision activate --name avo-lab
--resource-group rg-ntnu-ws-2026 --revision <name>` becomes available too.

---

## Changes that do NOT need a redeploy

`config.yaml` is baked into the image, but a lot of it is patchable live from
`/admin`. Check here before rebuilding:

| Change | Where |
|---|---|
| deal bonus, LLM concurrency | admin → Group stage → *Apply* |
| inter-message delay | admin → Playoffs → *Apply* |
| prompt-injection policy | admin → Group stage → checkbox |
| Gatekeeper round length, +1 min, stop | admin → Gatekeeper |
| submission window length, extend, close | admin → Submission window |
| a team's vault, name, prompt, benching | admin → Teams |

These apply to the running process and are **lost on restart** — so anything you
want permanent still has to be edited and redeployed beforehand.

Needs a redeploy: tier prompts, secret words, the practice-bot prompt, the rules
block, **the presentation slides in `slides.yaml`**, anything in `web/`, and any
Python change.

---

## Restart without rebuilding

If you only need to wipe state (a botched dry run, a stuck stage) and the code is
already correct, restart the revision instead of redeploying. Get its name:

```bash
az containerapp show --name avo-lab --resource-group rg-ntnu-ws-2026 --query "properties.latestRevisionName" -o tsv
```

Then restart it:

```bash
az containerapp revision restart --name avo-lab --resource-group rg-ntnu-ws-2026 --revision <revision-name>
```

Faster still, and safe to do mid-session: `/admin` → type `RESET` → *Wipe event*.
That clears the game without touching the container at all.

---

## Rotate a secret

```bash
az containerapp secret set --name avo-lab --resource-group rg-ntnu-ws-2026 --secrets azure-openai-key="<new key>" admin-code="<new admin code>"
```

Secrets do not take effect until the container restarts:

```bash
az containerapp update --name avo-lab --resource-group rg-ntnu-ws-2026 --image caec7709e914acr.azurecr.io/avo-lab:v5
```

Non-secret values (join code, deployment name) go through `--set-env-vars` on the
same `update` command.

---

## If `az` misbehaves

Wrong subscription is the usual cause:

```bash
az account set --subscription avo-internal-sandbox
```

Do **not** run `az extension add --name containerapp`. On Azure CLI 2.67 the
`containerapp` commands are already in the core CLI; the extension is obsolete
and its installer crashes pip on Windows with exit code `3221225477`.

---

## Tags in the registry

Newest first, as of the last check:

| tag | what it is |
|---|---|
| `v7` | **built, not yet deployed.** Persona vaults (Bjørn / Pia / Arne Benjamin) with photos, the Norwegian presentation phase, "Språkmodell hackathon" title, content-filter verdicts, Arne Benjamin softened twice |
| `v6` | **live.** Built outside this session on 2026-09-09 15:18; contents uncertain |
| `v5` | Tied ranks, tougher practice bot, build-window timer, practice-duel deal bonus, fixed projector captions, finalist prompt reveal |
| `gatekeeper-v2` | per-team vault ladder, +50 per breach, 15 minute round |
| `banner-fix` | public URLs in the startup banner, calibrated Gatekeeper tiers |
| `20260805162900945990` | the original `az containerapp up` build |

Not every change got its own tag — several were rolled together. Keep bumping
`v6`, `v7`, … so the tag says what is live at a glance.
