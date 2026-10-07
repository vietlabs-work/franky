# Resource use

Franky stores `/work`, `/home/franky`, and `/tmp` in disposable disk volumes. Only small runtime paths use tmpfs.

| Resource | Default | Configuration |
|----------|---------|---------------|
| Task tree, including nested containers | 2048 MiB RAM, no swap | `FRANKY_MEMORY_MB`, 256 through 8192 |
| Proxy | 128 MiB RAM, no swap | Fixed per task |
| Task disk data | 8192 MiB soft budget | `FRANKY_DISK_MB`, 1024 through 32768 |
| Disk helper | 64 MiB RAM | Short-lived and networkless |

For two jobs on a 16 GiB Mac, allocate at least 6 GiB to Docker Desktop. Start with one active job per caller.

Limits are ceilings, not reservations. The disk watchdog samples approximately every five seconds, so it is not a filesystem quota. Volume deletion is not secure erasure.

Docker shares common image layers across Franky instances. Engine-specific images omit the other engine clients.

Synthetic checks do not prove that an arbitrary repository or live model workload fits. Test your largest repository before increasing concurrency.
