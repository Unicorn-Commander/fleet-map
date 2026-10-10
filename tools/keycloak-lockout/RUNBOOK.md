# Keycloak brute-force lockout hardening

This runbook accompanies `kc_lockout.py`. It is a preparation and execution
guide; creating the files in this lane does not change any Keycloak realm.
The script uses Python 3 standard-library HTTP calls and does not require
PyYAML or another package.

## Scope and proposed settings

The example configuration contains five separately named targets:

| target name | host / base URL | realm | proposed change |
|---|---|---|---|
| `majiks-vps-master` | `auth.majiks.studio` | `master` | enable protection, failure factor 10, temporary lockout (`permanentLockout=false`) |
| `commander-uc-customers` | `auth.unicorncommander.ai` | `uc-customers` | enable protection, failure factor 12, temporary lockout |
| `commander-uchub` | `auth.unicorncommander.ai` | `uchub` | failure factor 12, three temporary lockouts, `permanentLockout=false`, 120-second wait increment |
| `bigboy-uchub` | `auth.magicunicorn.dev` | `uchub` | failure factor 12, three temporary lockouts, `permanentLockout=false`, 120-second wait increment |
| `majiks-realm` | `auth.majiks.studio` | `majiks` | failure factor 12, three temporary lockouts, `permanentLockout=false`, 120-second wait increment |

The example intentionally omits other supported brute-force keys where the
task did not specify a new number. Omitted keys are not changed. Supported
keys are `bruteForceProtected`, `failureFactor`, `permanentLockout`,
`maxTemporaryLockouts`, `waitIncrementSeconds`, `maxFailureWaitSeconds`,
`maxDeltaTime`, `minimumQuickLoginWaitSeconds`, and
`quickLoginCheckMilliSeconds`.

The `admin_user_env` and `admin_password_env` values are environment-variable
names, not credentials. A target may instead use `token_env` for a bearer-token
environment-variable name. Do not put credential values, tokens, SMTP
passwords, or realm exports in the target file or in task comments.

## Safety and order of operations

1. Make a working copy of `targets.example.json` on the host that can reach
   the relevant Keycloak endpoints. Keep it outside version control if it
   contains local operational changes. Set only the named environment
   variables on that host. The script reads no `.env` file.

2. Run a read-only plan for all five targets:

   ```sh
   cd tools/keycloak-lockout
   python3 kc_lockout.py plan --targets targets.json
   ```

   Review every row and every self-service-unlock warning. Exit code `0`
   means no configured difference; exit code `2` means at least one desired
   difference was found. Neither result issues a PUT. Exit code `1` means the
   configuration, login, transport, or response could not be checked.

3. Handle `majiks-vps-master` first because the 2026-09-28 measurement found
   brute-force protection disabled on the public Majiks master console. Before
   or alongside the realm change, obtain the separate approval and edge change
   to IP-restrict both `/realms/master` and `/admin` on
   `auth.majiks.studio`, using the same allow-list approach that
   `gen_kcmaster.py` used on commander and bigboy. This script does not make
   that network-policy change. Verify the restriction from an allowed network
   and an unapproved network before treating that gate as complete.

4. Apply exactly one named target per invocation, after the plan and the
   required approval:

   ```sh
   python3 kc_lockout.py apply \
     --targets targets.json \
     --realm majiks-vps-master \
     --yes
   ```

   Use the target names above for the two `uchub` instances; their realm name
   alone is ambiguous. The command GETs the full realm representation, writes
   it to `./backups/<target>-<UTC timestamp>.json` with mode `0600`, and only
   then PUTs the full representation with the configured brute-force keys
   merged in. It re-GETs the full realm and prints the before/after values.
   A no-op does not issue a PUT or create a backup.

5. Run the lockout test only with a pre-created, non-production throwaway user
   whose account owner has approved the test. Without `--yes`, `verify`
   prints the procedure and performs no HTTP request:

   ```sh
   python3 kc_lockout.py verify \
     --targets targets.json \
     --realm majiks-vps-master \
     --throwaway-user kc-lockout-probe
   ```

   With `--yes`, the script uses `failureFactor + 1` wrong-password attempts
   by default, or an explicit positive `--attempts`, checks for a lockout
   response and the admin attack-detection state, and always attempts the
   documented cleanup DELETE. The default public client is `account-console`;
   pass `--client-id` if the realm uses another public client with Direct
   Access Grants enabled. A successful verification must show lockout
   evidence and a successful cleanup.

6. Repeat the plan after each approved apply, then continue in this order:
   `commander-uc-customers`, `commander-uchub`, `bigboy-uchub`, and
   `majiks-realm`. Do not batch the apply operation across realms.

## Running on each host

The command may run on the host where the target is reachable; it does not
SSH, restart a service, run Docker, or select a host automatically. Keep the
same script and reviewed target file on each host, and provide only that
host's credential environment variables:

| host | targets to check/apply |
|---|---|
| majiks-vps | `majiks-vps-master`, then `majiks-realm` |
| commander | `commander-uc-customers`, then `commander-uchub` |
| bigboy | `bigboy-uchub` |

Use the same `plan` command before each host-local apply. If a URL or realm
does not match the reviewed inventory, stop and correct the target file
before using `--yes`.

## Rollback

The backup is the full representation captured immediately before the PUT.
Treat it as sensitive because a realm export can include SMTP configuration or
other operational values. Do not paste its contents into the task, commit it,
or leave it world-readable.

After identifying the exact backup and confirming the target URL and realm,
obtain a fresh bearer token and PUT that backup back to the matching admin
realm endpoint. For example, with the token held only in an environment
variable and the backup path selected explicitly:

```sh
curl --fail-with-body --silent --show-error \
  -X PUT \
  -H "Authorization: Bearer ${KC_ROLLBACK_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data-binary '@backups/REVIEWED-TARGET-UTC-TIMESTAMP.json' \
  'https://REVIEWED-BASE-URL/admin/realms/REVIEWED-REALM'
```

Do not substitute a wildcard or an unreviewed backup. Re-GET the full realm,
compare it with the backup, and run `plan` before any subsequent hardening.
Rollback restores the captured full representation, so it may also restore
unrelated realm settings changed before the backup; review the diff first.

## Evidence to paste on the task

For each target, paste only non-secret evidence:

- host, UTC timestamp, target name, realm, and the reviewed script/config
  commit;
- the read-only `plan` output showing the before/current and desired
  brute-force values, its exit code, and all unlock-path warnings;
- the apply output showing the backup filename, before values, after values,
  and the successful full-representation re-read;
- a checksum and permission check for the retained backup, without pasting its
  contents;
- the `verify --yes` output showing the attempt count, lockout evidence, HTTP
  statuses, and successful DELETE cleanup; and
- separate evidence for the Majiks edge restriction: the approved-network
  result and the denied-network result for `/realms/master` and `/admin`.

Never paste access tokens, passwords, SMTP credentials, full realm backups, or
raw error bodies into the task.
