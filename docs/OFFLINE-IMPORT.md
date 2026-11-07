# Offline image import

## The problem

You have the bytes of an OCI image but no fast route to the registry:

```
$ time tart clone ghcr.io/cirruslabs/debian:latest agent
# ... 600 MB at ~35 KB/s aggregate = 4.8 hours
```

Measured on the host this project was built for:

| route | speed |
|---|---|
| `deb.debian.org` | 38 KB/s |
| `ghcr.io` single connection | 2.7 KB/s |
| `ghcr.io` via Tart's concurrency | ~35 KB/s |
| Tsinghua Debian mirror | 12.5 MB/s |

Meanwhile `api.github.com` works fine and `codeload.github.com` works fine. So
the bytes are reachable — just not from the registry.

## Why you cannot just drop files into Tart's cache

The obvious move is to download the layers and put them where Tart looks. It does
not work. `tart clone` streams OCI layers straight into
`~/.tart/tmp/<uuid>/disk.img`; the content-addressed cache under
`~/.tart/cache/content/sha256/` is not consulted on this path. You can populate
it perfectly and Tart will still try to download.

## The fix: serve them, let Tart pull normally

`scripts/lib/oci-registry.py` implements the three endpoints Tart actually uses:

```
GET /v2/                        -> 200          # spec-mandated ping
GET /v2/<name>/manifests/latest -> manifest
GET /v2/<name>/blobs/<digest>   -> blob bytes
```

Blob responses support `HEAD` and `Range`, because Tart's fetcher issues range
requests when downloading layers concurrently.

Only digests referenced by `manifest.json` are served. A half-populated
directory therefore fails with `404 BLOB_UNKNOWN` instead of silently handing
Tart a truncated layer — the difference between a clear error and a corrupt disk
image.

## Flow

```sh
# 1. download every referenced object, resumably
scripts/agentvm-fetch --repo ghcr.io/cirruslabs/debian --tag latest --verify

# 2. serve them
scripts/agentvm-serve --port 5000

# 3. let Tart pull from localhost (--insecure is mandatory for plain HTTP)
tart clone 127.0.0.1:5000/debian:latest agent --insecure --concurrency 16
```

## Getting the bytes when the registry is throttled

`agentvm-fetch` handles the normal case. When the registry is the problem, use
whatever route *does* work and place the files in the layout it expects:

```
image/
  manifest.json
  blobs/sha256/<digest>      # one file per object
  blobs.txt                  # produced by agentvm-fetch: "TYPE DIGEST SIZE"
```

Two things make this robust:

**Duplicate digests collapse.** A manifest can reference the same layer many
times — the Debian image has 40 layer entries but only 18 unique digests, because
a 3.1 MB layer appears a dozen times. One file on disk, not twelve.

**Content addressing is free verification.** Because a blob's filename *is* its
SHA-256, `--verify` re-hashing each file and comparing to its name is proof the
bytes are intact. That is a stronger check than any checksum you could paste
alongside a download.

## Transferring a large image over a slow link

If you have to move the image between machines, splitting it and shipping the
parts separately is much more robust than one big transfer:

```sh
split -b 20M image.tar.gz image.tar.gz.part
# ... send parts, resume the ones that fail ...
cat image.tar.gz.part* > image.tar.gz
shasum -a 256 image.tar.gz     # compare against the value from the source
tar tzf image.tar.gz           # list without extracting
```

Compression helps a lot here: the Debian image's 20 GB logical disk is mostly
zeros, so 605 MB of layers compressed to 442 MB, and the repeated 3.1 MB layer
dropped from 3.1 MB to ~14 KB.
