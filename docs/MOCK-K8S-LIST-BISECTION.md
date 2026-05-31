# Bisecting the `ListServerSetScales` null-key crash

A runbook for the one remaining mock-k8s mystery: the Funcom Battlegroup
Director crashes on startup when our K8s shim returns a **non-empty**
`ListServerSetScales` response, so mock-k8s returns an empty list by
default.

It can only be completed against a **live Director** (a real panel + the
extracted depot) — it cannot be reproduced from the Go tests. The tooling
below turns that bisection into a controlled experiment instead of a guess.

## What we know

- Director discovers maps from the BattleGroup spec, then `GET`s each
  ServerSetScale **by name** and `PATCH`es scale changes. It does **not**
  need `LIST` results for any feature exercised so far.
- A by-name `GET` of a ServerSetScale is **accepted**. The same object
  inside a `LIST` envelope **crashes** Director with `ArgumentNullException`
  from a `Dictionary.Add(null_key)` while it deserializes the list.
- Because a single item is fine on its own, the fault is in the
  *collection → dictionary* step: Director keys the dictionary by a
  per-item string property that resolves to `null` for at least one item.
- At startup the throw prevents Director from opening port `11717`, which
  fails `start-director.sh`'s `wait_for_port` and kills the container.
  Hence the empty-list workaround.

## What mock-k8s gives you

Two env vars, both read **per request** so you can change them live (edit
the egg variable / container env and re-trigger a Director restart — no
mock-k8s rebuild needed):

| Env | Effect |
|-----|--------|
| `MOCK_K8S_LIST_ENABLE=1` | Return the **real** items in `LIST` instead of an empty array. Each item is shape-normalised first so it carries the same non-null fields a by-name `GET` does (`metadata.labels` with `igw.funcom.com/map-name` + `igw.funcom.com/battlegroup-name`, a `status` block, `spec`). |
| `MOCK_K8S_LIST_OMIT=a,b.c` | Drop the listed dotted fields from each `LIST` item before sending. A dotted path descends one map level per dot, to any depth (`status`, `metadata.labels`, `metadata.labels.existing`). Remove suspected fields one at a time. |

Every enabled `LIST` is logged:

```
[mock-k8s] [INFO] serversetscale: LIST returning real items (MOCK_K8S_LIST_ENABLE set) namespace=default count=3 omit=[status]
```

## Procedure

1. **Confirm the baseline crash.** `MOCK_K8S_LIST_ENABLE=1`, no
   `MOCK_K8S_LIST_OMIT`, restart. Director should crash before `11717`
   opens. Capture `logs/director.log`.

2. **Halve the surface.** The dictionary key is a string, so start with the
   string-bearing maps:

   ```
   MOCK_K8S_LIST_OMIT=metadata.labels,metadata.annotations
   ```

   Crash gone → the key lived there. Crash stays → it's in `spec`,
   `status`, or a top-level field.

3. **Narrow to one field.** Shrink the omit list one field at a time until
   the crash returns. The last field you re-added is the culprit.

4. **Inspect that field.** With the culprit known, look at what mock-k8s
   emits for it (the enabled-LIST log line + a manual `curl` inside the
   container). Populate it with a non-null value for **every** item — most
   likely in `ensureUniformItem` (`internal/serversetscale/handler.go`) or
   at lazy-create time (`store.go` `GetOrLazyCreate`).

5. **Verify and lift the workaround.** Once `MOCK_K8S_LIST_ENABLE=1` boots
   cleanly with **no** omits, the empty-list default in `handleList` can be
   removed so `LIST` works unconditionally. Add a regression test in
   `handler_test.go` asserting the now-required field is present on every
   item.

## Manual curl from inside the container

```bash
TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
curl -sk -H "Authorization: Bearer $TOKEN" \
  https://127.0.0.1:6443/apis/igw.funcom.com/v1/namespaces/default/serversetscales \
  | python3 -m json.tool
```

Compare an item here against the by-name `GET`
(`.../serversetscales/<name>`) — they should be identical per item; any
difference is a lead.

## Notes

- Keep `MOCK_K8S_LIST_ENABLE` **unset in production** until this is
  solved — the empty-list default is the known-good, player-tested state.
- The watch stream (`?watch=true`) already emits real `ADDED` events for
  existing objects; if the crash ever appears on the watch path too, the
  culprit field is shared and fixing `ensureUniformItem` covers both.
