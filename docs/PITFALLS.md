# Pitfalls

Every entry here cost real time. They are ordered roughly by how surprising they
were. If you only read one section, read "Secrets get mangled in transit" — it
fails silently and looks like a bad API key.

---

## 1. Secrets get mangled in transit

**Symptom.** You write a perfectly good API secret into a config file. The API
says `invalid credentials`. You test the same secret by hand with `curl` and it
works. You conclude the config file is being ignored, and start debugging the
loader.

**Cause.** When a secret is copied through a chat interface, a CI log, or any
pipeline with a redaction layer, that layer may replace the *first characters*
of the key with a placeholder token while leaving the rest intact. The result is
written to disk verbatim. The file looks plausible; only the byte count is wrong.

In this project it turned a 35-character secret into a 44-character string with
an 18-character placeholder glued onto the front — same tail, wrong prefix:

```
intended:  AbCdEf1234567890abcdefghijklmnopqr
written:   <PLACEHOLDER_x7f3a9>abcdefghijklmnopqr
```

(The real values are deliberately not reproduced here. Use your own synthetic
example when documenting this class of bug.)

**How it was found.** A direct API call with the hand-typed secret returned
`code: 0`, while the gateway running the file's contents returned
`app_id or app_secret is invalid`. That contradiction ruled out every
configuration theory and forced a byte-level look at the file:

```sh
grep "^FEISHU_APP_SECRET=" ~/.hermes/.env | od -c | head -4
```

**Fix / prevention.**

- Transport secrets as base64, never as literal text.
- Assert the byte length before and after transfer.
- Prefer reconstructing a value from a file you already have over retyping it.
- Verify by reading the value *back out of the file* and using it, not by
  re-testing the value you think you wrote.

`scripts/50-configure.sh` does all of this: it reads secrets from files, pipes
them as base64, and greps for the expected length on the far side.

---

## 2. `tart exec` fails with exit 125 and no error message

**Symptom.**

```sh
$ tart exec agent -- echo hello
$ echo $?
125
```

No output, no diagnostic. It looks like the VM is dead or the guest agent is not
running.

**Cause.** The syntax is `tart exec <name> <command>...`. The `--` is *not* a
separator that gets stripped — it is passed through as the command name, so Tart
tries to execute a binary called `--`.

**Fix.**

```sh
tart exec agent echo hello        # correct
tart exec agent sh -c 'nproc'     # correct
```

This one is nasty because the failure is silent and the exit code is generic. If
`tart exec` returns 125, try removing `--` before investigating anything else.

---

## 3. `HERMES_YOLO_MODE` is frozen at import time

**Symptom.** You set `HERMES_YOLO_MODE=1` in `.env`, restart the service, and
`is_approval_bypass_active()` still reports `False` when you check it by hand.

**Cause.** `tools/approval.py` reads it once, at module import:

```python
_YOLO_MODE_FROZEN: bool = is_truthy_value(os.getenv("HERMES_YOLO_MODE", ""))
```

`.env` is loaded by python-dotenv *later*, during application startup. Whether
the variable is visible depends on import order, which differs between running
the CLI directly and running under systemd.

The comment in the source says this is deliberate — freezing it closes a
prompt-injection escalation path where a skill flips the flag mid-process.

**Fix.** Use the config path as the primary mechanism and the env var as backup:

```yaml
# ~/.hermes/config.yaml
approvals:
  mode: "off"        # read at call time via load_config()
```

Plus a systemd drop-in so the service definitely has the variable:

```ini
# /etc/systemd/system/hermes-gateway.service.d/override.conf
[Service]
Environment=HERMES_YOLO_MODE=1
```

Verify with:

```sh
./venv/bin/python -c "from tools.approval import is_approval_bypass_active, _get_approval_mode; print(is_approval_bypass_active(), _get_approval_mode())"
```

**Also note:** bypassing approvals is not the same as removing all limits. The
source comments state that hardline blocks are enforced by the caller *before*
this gate, so `--yolo` skips only the recoverable approval layer.

---

## 4. YAML 1.1 turns `off` into a boolean

**Symptom.** You write `approvals: {mode: off}` and approvals still behave as
`manual`.

**Cause.** YAML 1.1 treats bare `off`/`no`/`false` as booleans, so the value
arrives in Python as `False`, not the string `"off"`.

**Fix.** Quote it: `mode: "off"`. The agent's own normaliser handles both, but
quoting removes the ambiguity.

---

## 5. Only one of the two auto-start layers was configured

**Symptom.** Everything works, you reboot, and the bot is silent.

**Cause.** There are two independent things to start and they live in different
operating systems:

| layer | what | mechanism |
|---|---|---|
| host | the VM | launchd, `RunAtLoad` + `KeepAlive` |
| guest | the agent gateway | systemd, `enabled` |

Configuring only the guest service means nothing starts it, because the VM is
not running. Configuring only the host means the VM boots but the gateway does
not.

**Fix.** Do both. `scripts/60-autostart.sh` handles the pair. Verify the whole
chain, not just one hop:

```sh
launchctl list | grep com.agentvm
tart list
tart exec agent systemctl is-active hermes-gateway
```

---

## 6. `tart clone` ignores local blob caches

**Symptom.** You download image layers by hand, drop them into
`~/.tart/cache/content/sha256/`, and `tart clone` still tries to download from
the registry.

**Cause.** `tart clone` streams OCI layers directly into
`~/.tart/tmp/<uuid>/disk.img`. The content-addressed cache is not consulted for
this path.

**Fix.** Serve the blobs over HTTP and let Tart pull normally. That is what
`scripts/lib/oci-registry.py` is for. Note that `--insecure` is mandatory:
Tart refuses a plain-HTTP registry without it.

---

## 7. Disk size can only grow

**Symptom.** You want a 10 GB guest; `tart set --disk-size 10` fails or does
nothing.

**Cause.** Tart's images carry a fixed logical size in the manifest annotation
`org.cirruslabs.tart.uncompressed-disk-size` — 20 GB for `cirruslabs/*`. The
setter refuses to shrink, to avoid data loss.

**Reality check.** Logical size is not disk usage. The image is APFS-sparse, so a
20 GB logical Debian guest occupies 2–3 GB on disk. If you genuinely need a
small *logical* disk, you need `tart create` plus an OS install, not a clone.

---

## 8. Guest takes ~90 seconds before it answers

**Symptom.** `tart list` says `running`, but SSH and `tart exec` both fail. You
conclude the VM is broken, restart it, and get the same thing.

**Cause.** `tart run` returns as soon as Virtualization.framework accepts the
configuration. The guest kernel and userspace are still booting.

**Fix.** Wait. Poll instead of guessing:

```sh
for i in $(seq 1 30); do
  tart exec agent true 2>/dev/null && break
  sleep 10
done
```

---

## 9. Registry throttling looks like a hung download

**Symptom.** A 600 MB image download sits at the same byte count for minutes.

**Cause.** Two different failure modes look identical:

- **Throttling.** Traffic is flowing, just very slowly (measured: 2.7 KB/s on a
  single connection to ghcr.io, ~35 KB/s aggregate with Tart's concurrency).
- **Stall.** Nothing is flowing.

**How to tell them apart.** Sample the same counter twice:

```sh
nettop -P -l 2 -p $(pgrep -f "tart clone" | head -1)
```

`bytes_in` is cumulative, so two samples a second apart give you a real rate.
A `dd` read test is misleading here: the page cache will happily serve 771 MB/s
from a file that was just written, telling you nothing about the disk.

**Fix.** Don't fight it. Download by another route and use the offline import
path (pitfall 6).

---

## 10. `cp` on a sparse disk image fills your disk

**Symptom.** You copy a 64 GiB *logical* disk image to check something, and your
disk fills up.

**Cause.** Plain `cp` materialises holes. A sparse file that occupies 28 GiB
becomes a full 64 GiB.

**Fix.** Use `dd conv=sparse`, or `rsync --sparse`:

```sh
dd if=src.img of=dst.img bs=4m conv=sparse
```

Verify afterwards that the destination is still sparse:

```sh
stat -f "logical %z, allocated %b*512" dst.img
```

Also note that `mv` between APFS volumes does *not* preserve sparseness and
preallocates the full size at the destination, and `ditto` materialised it too.
`dd conv=sparse` was the only tool here that got it right.

---

## 11. Killing a shell wrapper leaves the real child running

**Symptom.** You `kill` a long-running `mv`/`cp`, confirm it is gone, and hours
later find the same process still writing to disk.

**Cause.** `pgrep -f "mv ..."` matches the `zsh -c` wrapper *and* the `mv` child.
Killing the wrapper's PID leaves the child orphaned and reparented to launchd,
where it keeps running.

**Fix.** Match the actual binary and kill everything that matches:

```sh
pkill -9 -f "^mv "          # anchored, matches the real process
pgrep -fl "mv -PRpv"        # verify with the distinctive flags
```

Orphaned copies of large files are invisible in `df` if you deleted the
destination, because the blocks are held by the open file descriptor.

---

## 12. `HTTP 402` is a billing problem, not a config problem

**Symptom.** The gateway reports a generic "the model provider failed after
retries" and hides the upstream error.

**Cause.** The gateway deliberately keeps raw provider errors out of chat. The
real error is only in the logs:

```sh
sudo journalctl -u hermes-gateway -n 50 | grep -iE "error|402"
```

`402 insufficient_balance` means the key authenticated, the endpoint was right,
the model name was accepted — the account is just empty. Changing configuration
cannot fix it.

**Side effect worth knowing.** A 402 also marks the auxiliary client (title
generation) unhealthy for 600 s, so even after topping up you should restart the
service rather than waiting it out.
