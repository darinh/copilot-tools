# The CLI's `experimental` default, measured

**Measured OFF.** With no `experimental` key in `~/.copilot/settings.json`, the
Copilot CLI loads **no** runtime extensions. Windows, CLI **1.0.77**,
2026-08-01.

The honest claim is *"measured OFF on 1.0.77/Windows"*, not *"the default is
OFF"*. `copilot --help` documents no default, so there is nothing here the CLI
has promised to keep doing — see [Scope](#scope-and-what-would-overturn-this).

This file is the *measurement record* — the method, the evidence and the
reproduction. What the result means for `setup_tools --status` and for the
guard is documented in [checkout-guard.md](checkout-guard.md); this file
deliberately does not restate it.

## Why it was open

`copilot --help` documents `--experimental` and `--no-experimental` and **no
default**, so the CLI's built-in behaviour was never ours to assert.
`setup_tools.extension_mode()` therefore reports an absent key as
`UNDETERMINED` rather than guessing, and `docs/checkout-guard.md` carried a
hedge naming the cost of that in advance — retired by this measurement, so do
not expect to find it there now:

> if the CLI's own default turns out to be experimental-off, a machine with no
> `experimental` key is inert and this command will not say so

Turning that `if` into an `is` needs a measurement, not a reading of the help
text. This is that measurement.

## Method

A throwaway `HOME` per case, so the machine's real, sticky
`settings.json` is never read and never written:

- a fresh directory containing only `.copilot/config.json`, copied across
  because it carries authentication — **`settings.json` is not copied**, it is
  controlled per case;
- a probe extension at `<HOME>/.copilot/extensions/probe/extension.mjs`
  that writes a marker file **at module-evaluation time, before any SDK
  import**, so the marker proves the CLI evaluated the module and its absence
  proves it did not. The marker path is an absolute path baked into the file
  when it is written, so the probe depends on no environment variable that the
  case under test could perturb;
- `USERPROFILE` and `HOME` pointed at that directory, then
  `copilot -p ping --allow-all-tools --no-ask-user --no-remote
  --no-remote-export -s`.

**Every run exited 0 with a real model reply**, so "the session never started"
does not explain any negative result.

Two independent observables were read, one of ours and one of the CLI's:

1. the probe's marker file;
2. whether the CLI created `<HOME>/.copilot/logs/extensions/` at all — per
   checkout-guard.md, no log for a launch means extensions were never
   attempted. Nothing we wrote produces this signal.

## Results

| `settings.json` seeded | flag | marker | CLI ext. log dir | `settings.json` afterwards |
|---|---|---|---|---|
| *absent* | `--experimental` | **written** | 1 log | created, `{"experimental": true}` |
| *absent* | *none* | absent | never created | **still absent** |
| *absent* | `--no-experimental` | absent | never created | created, `{"experimental": false}` |
| `{model, showReasoning}` | *none* | absent | never created | **unchanged — key not added** |
| `{model, showReasoning}` | `--experimental` | **written** | 1 log | key appended, `true` |
| `{model, showReasoning}` | *none* (replicate) | absent | never created | unchanged |

The two observables agree on all six runs.

### Why the negatives are believable

Row 4 is the case that matters: it is the exact branch `extension_mode()`
reports as `UNDETERMINED` — settings file present, key missing.

It has a **matched positive control** in row 5. Same seeded `settings.json`,
same extension file, same throwaway `HOME`, same command; only the flag
differs, and the flag decides.

That one control retires both serious alternative explanations at once. Without
it, "seeding `settings.json` broke the extension loader" would explain row 4
exactly as well as "the default is off" does — and so would the sharper
objection, that redirecting `HOME`/`USERPROFILE` to a synthetic directory is
itself what stopped extensions loading. Row 5 runs under that same synthetic
`HOME`, with that same seeded file, and loads the extension. Neither the
redirection nor the seeding can be what silenced row 4.

Row 6 repeats row 4 and is the determinism check: the result is stable, not a
flake.

## Two findings beyond the yes/no

**The CLI writes nothing when given no flag.** Rows 2, 4 and 6 left
`settings.json` exactly as they found it — absent stayed absent, and a file
without the key did not acquire one. An absent key is therefore not a transient
state that resolves on first run. It is the permanent steady state of a machine
where nobody has ever passed a spelling explicitly, which is to say: a fresh
install.

**Absent-key is behaviourally indistinguishable from `false`.** Rows 2/4/6 and
row 3 produced identical results on both observables.

## Scope, and what would overturn this

One platform (Windows), one CLI version (1.0.77), one probe. `copilot --help`
still documents no default, so this is *measured behaviour, not a documented
contract* — `copilot update` can move it without notice, and nothing here would
warn you. Anything asserting the default should cite this measurement rather
than claim the CLI guarantees it.

Re-measure after a CLI update. The reproduction is below; it starts six real
CLI sessions and takes a few minutes.

## Reproduction

```powershell
$scratch  = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ([guid]::NewGuid())) |
            Select-Object -ExpandProperty FullName
# Captured ONCE, before any case mutates $env:USERPROFILE. Read it inside the
# function instead and the second case copies its auth from the first case's
# fake home, which happens to work and is not what you meant to test.
$realHome = $env:USERPROFILE

function Measure-ExperimentalDefault {
    param([string]$CaseName, [string[]]$ExtraArgs = @(), [string]$SeedSettings = '')

    $fakeHome = Join-Path $scratch "home-$CaseName"
    $marker   = Join-Path $scratch "marker-$CaseName.json"
    New-Item -ItemType Directory -Force -Path (Join-Path $fakeHome '.copilot\extensions\probe') | Out-Null

    # auth only; settings.json is controlled per case
    Copy-Item (Join-Path $realHome '.copilot\config.json') (Join-Path $fakeHome '.copilot\config.json')
    if ($SeedSettings) {
        Set-Content (Join-Path $fakeHome '.copilot\settings.json') $SeedSettings -Encoding utf8
    }

    # marker is written before any SDK import, so it proves module evaluation
    @"
import { writeFileSync } from "node:fs";
writeFileSync(String.raw``$marker``, new Date().toISOString(), "utf-8");
try {
  const { approveAll } = await import("@github/copilot-sdk");
  const { joinSession } = await import("@github/copilot-sdk/extension");
  await joinSession({ onPermissionRequest: approveAll, tools: [] });
} catch (err) { process.stderr.write("probe: " + err.message + "\n"); }
"@ | Set-Content (Join-Path $fakeHome '.copilot\extensions\probe\extension.mjs') -Encoding utf8

    $env:USERPROFILE = $fakeHome
    $env:HOME        = $fakeHome
    $cliArgs = @('-p','ping','--allow-all-tools','--no-ask-user','--no-remote','--no-remote-export','-s') + $ExtraArgs
    $p = Start-Process (Get-Command copilot).Source -ArgumentList $cliArgs -NoNewWindow -PassThru `
         -RedirectStandardOutput (Join-Path $scratch "out-$CaseName.txt") `
         -RedirectStandardError  (Join-Path $scratch "err-$CaseName.txt")

    # Required. Windows PowerShell 5.1 leaves .ExitCode $null on a -PassThru
    # process without it, however you wait, so the check below would read
    # blank and pass. See "The exit-code check is load-bearing" below.
    $p.EnableRaisingEvents = $true
    if (-not $p.WaitForExit(240000)) { $p.Kill(); throw "$CaseName timed out" }

    # A run that did not complete cannot testify about extension loading:
    # it produces exactly the same "no marker" as a session that loaded none.
    if ($p.ExitCode -ne 0) {
        throw "$CaseName exited [$($p.ExitCode)] -- proves nothing. See err-$CaseName.txt"
    }

    [pscustomobject]@{
        case     = $CaseName
        exit     = $p.ExitCode
        loaded   = Test-Path $marker
        extLogs  = Test-Path (Join-Path $fakeHome '.copilot\logs\extensions')
        settings = Get-Content (Join-Path $fakeHome '.copilot\settings.json') -Raw -EA SilentlyContinue
    }
}
```

### The exit-code check is load-bearing

A failed run and a run where extensions did not load are the *same
observation*: no marker. So the exit code is the only thing separating "the
default is off" from "the command never ran", and it has to be able to fail.

Both halves of that were found the hard way while writing this file. The first
attempt passed the prompt unquoted, so the CLI rejected the invocation and
exited 1 with no session at all — which looked exactly like a clean negative.
The second attempt reported the exit code as blank on stock `powershell.exe`,
because `Start-Process -PassThru` leaves `.ExitCode` `$null` under Windows
PowerShell 5.1 unless `EnableRaisingEvents` is set. A control that reads blank
does not fail; it just stops being a control.

Prove it can still fire before trusting a negative:

```powershell
Measure-ExperimentalDefault 'guard-check' -ExtraArgs '--definitely-not-a-flag'
# must THROW "exited [1] -- proves nothing". If it returns a row, the guard is dead.
```

Every code block here is deliberately **ASCII-only**, and should stay that way.
An em dash inside a PowerShell string is not cosmetic: saved as UTF-8 without a
BOM and read by Windows PowerShell 5.1 as ANSI, `—` becomes `â€”`, whose third
character `”` is a *string delimiter* in PowerShell. The string terminates
early and the whole function fails to parse. That is how this very guard failed
its first run.

Run each case in its **own** `$fakeHome` — the CLI persists the flag it was
given, so reusing one contaminates the next case. Run the positive control
first: if a known-`--experimental` run does not write the marker, the probe is
broken and every negative below it is meaningless.

```powershell
$seed = '{"model":"claude-opus-5","showReasoning":false}'
Measure-ExperimentalDefault 'control'          -ExtraArgs '--experimental'                    # expect loaded = True
Measure-ExperimentalDefault 'default'                                                          # the question
Measure-ExperimentalDefault 'negative'         -ExtraArgs '--no-experimental'                  # expect loaded = False
Measure-ExperimentalDefault 'seeded'           -SeedSettings $seed                             # the decisive case
Measure-ExperimentalDefault 'seeded-control'   -SeedSettings $seed -ExtraArgs '--experimental' # matched positive
Measure-ExperimentalDefault 'seeded-replicate' -SeedSettings $seed                             # determinism check
```

Those six calls are the six rows of the results table, in order.

Each `Measure-ExperimentalDefault` call replaces `USERPROFILE`/`HOME` in the
**calling** shell. Run the set in a throwaway shell, never in one you intend to
keep using.
