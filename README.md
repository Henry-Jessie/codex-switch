# codex-switch

Manage multiple [OpenAI Codex CLI](https://github.com/openai/codex) accounts from the command line.

If you use multiple ChatGPT Pro/Plus subscriptions for Codex CLI — for parallel batch evaluation, team usage, or quota management — this tool lets you switch between accounts, check real-time quota, and validate token health without manually copying `auth.json` files.

## Install

```bash
# With uv (recommended)
uv tool install codex-switch

# Or with pip
pip install codex-switch
```

Requires Python 3.11+ and [Codex CLI](https://github.com/openai/codex) installed.

## Quick Start

```bash
# Save your current logged-in account
codex-switch save work

# Import another auth.json
codex-switch add ~/Downloads/auth.json personal

# See all accounts at a glance
codex-switch list

# Switch to a different account
codex-switch switch personal
```

## Commands

### `codex-switch list`

Show all saved accounts with plan, quota, token expiry, and live API status:

```
Saved Codex accounts
   name      email                         plan  quota            access_exp   live
-  --------  ----------------------------  ----  ---------------  -----------  ----
*  pro1      user1@example.com             pro   5h:69% / wk:55%  04-10 10:48  ok
   pro2      user2@example.com             pro   5h:0% / wk:6%    04-13 16:27  ok
   personal  user3@gmail.com               pro   5h:0% / wk:0%    04-13 16:18  ok
```

- `*` = currently active account (matches `~/.codex/auth.json`)
- `5h` = 5-hour rolling window usage, `wk` = 7-day rolling window usage
- `access_exp` = when the access token expires (local time)
- `live` = real-time API validation result

### `codex-switch switch <name>`

Switch the active Codex CLI account:

```bash
$ codex-switch switch pro2
Switched to account 'pro2'
Active auth: /home/user/.codex/auth.json
```

### `codex-switch quota [name]`

Check real-time quota for the current or a specific account. Does not consume any API quota:

```bash
$ codex-switch quota pro1
Quota for pro1
email: user1@example.com
plan:  pro
auth:  chatgpt

bucket           name                 5h                 week               credits  plan
---------------  -------------------  -----------------  -----------------  -------  ----
codex            -                    70% (04-03 19:52)  55% (04-10 00:34)  0        pro
codex_bengalfox  GPT-5.3-Codex-Spark  0% (04-03 23:05)   0% (04-10 18:05)   -        pro
```

### `codex-switch validate [name]`

Check token health for one account or all saved accounts:

```bash
$ codex-switch validate pro2
pro2
  email:   user2@example.com
  plan:    pro
  access:  ok (2026-04-13 08:27)
  id:      expired (2026-04-03 09:27)
  refresh: present
  live:    OK 5h:0% / wk:6%
```

### `codex-switch current`

Show details about the currently active account:

```bash
$ codex-switch current
Current Codex account
name:  pro1
path:  /home/user/.codex/auth.json
email: user1@example.com
plan:  pro
access token: ok (2026-04-10 02:48)
id token:     expired (2026-03-31 03:48)
refresh:      present
```

### `codex-switch save <name>`

Save the current `~/.codex/auth.json` as a named account:

```bash
$ codex-switch save work
Saved current auth as 'work'
```

### `codex-switch add <path> <name>`

Import an auth.json file as a named account:

```bash
$ codex-switch add ~/Downloads/auth.json team-account
Imported account 'team-account'
```

### `codex-switch remove <name>` (alias: `rm`)

Remove a saved account. Cannot remove the currently active account:

```bash
$ codex-switch remove old-account
Removed account 'old-account'
```

### `codex-switch rename <old> <new>` (alias: `mv`)

Rename a saved account:

```bash
$ codex-switch rename team-account team
Renamed 'team-account' → 'team'
```

## How it works

Codex CLI stores authentication in `~/.codex/auth.json`. This tool maintains named copies in `~/.codex-switch/` and swaps them when you switch accounts.

Quota checking starts a temporary `codex app-server` process pointed at the target account, queries the `account/rateLimits/read` JSON-RPC endpoint, and shuts down. No Codex API quota is consumed.

### Storage layout

```
~/.codex-switch/
├── pro1.json        # saved auth.json for "pro1"
├── pro2.json        # saved auth.json for "pro2"
```

## Environment variables

For testing or custom setups:

- `CODEX_SWITCH_DATA_DIR` — override the account storage directory (default: `~/.codex-switch`)
- `CODEX_SWITCH_CODEX_HOME` — override the Codex home directory (default: `~/.codex`)

## License

MIT
