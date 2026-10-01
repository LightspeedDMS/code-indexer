---
name: project_eks_is_eventual_deployment_target
description: "Containerized EKS is the stated eventual deployment target — invalidates on-prem HA storage designs (DRBD/Pacemaker), rules out EFS, and revalidates the already-built OntapCloneBackend/FSx path"
metadata: 
  node_type: memory
  type: project
  originSessionId: 67868742-66ac-4b89-9d71-0e1ba3326cde
  modified: 2026-08-14T17:03:53.824Z
---

Stated direction (2026-08-14, user, in architecture discussion — a DIRECTION, not a committed dated plan):
the eventual deployment target is a **containerized deployment on AWS EKS**. Weigh every storage and
cluster-topology decision against that destination, not against the current 3-VM staging cluster.

**Why it matters:** it changes the answer to "what shared filesystem should we use," which otherwise
looks like an on-prem question. Three consequences established while working through the option space:

1. **On-prem HA storage designs are dead ends — do not propose them.** DRBD + Pacemaker/corosync is
   structurally wrong in EKS: out-of-tree kernel module on managed AMIs (re-broken on node rotation), a
   second cluster manager competing with Kubernetes, requires stable node identity + floating VIPs when
   EKS nodes are ASG cattle, and STONITH has no K8s equivalent. The legitimate K8s wrapper
   (Piraeus/LINSTOR CSI over DRBD) yields RWO block volumes, not a shared filesystem, so it fails the
   "all nodes see the same files" requirement anyway. Same verdict for self-run Rook-Ceph/Longhorn:
   neither has reflink.

2. **AWS EFS is a TRAP, despite being the default RWX StorageClass.** It is NFSv4.0/4.1 ONLY — there is
   no NFSv3 option, therefore no `nolock`, so the exact remedy Bug #1510 required is unavailable and
   NFSv4's integrated lock state machine (the confirmed cause of `disk I/O error` on `chunks.db` writes)
   becomes mandatory. It also has no reflink (NFS has no clone operation in any version; EFS caps at 4.1
   so not even v4.2 server-side COPY), which kills fast clones outright. Expect someone to propose it;
   the version cap alone is decisive. See [[feedback_no_fallbacks_ever]] for why "degrade to full copy"
   is not an acceptable answer here.

3. **The production storage backend is ALREADY IMPLEMENTED — target FSx, not a new filesystem.**
   `OntapCloneBackend` + `OntapFlexCloneClient` (Epic #408) and `clone_backend: "local" | "ontap" |
   "cow-daemon"` exist and are wired in `startup/clone_backend_wiring.py`. The CoW daemon was always
   the dev/non-prod SUBSTITUTE for ONTAP/FSx (see [[reference_cow_daemon_architecture]]), so FSx for
   NetApp ONTAP needs zero new code: managed multi-AZ HA, FlexClone over the same REST API, and NFSv3
   supported so `nolock` stays available. FSx for OpenZFS is the cheaper alternative
   (`CreateVolume` + `OriginSnapshot` + `CopyStrategy=CLONE` = instant writable CoW, NFSv3 supported)
   and would need one new `CloneBackend` class. JuiceFS is a viable fallback if FSx pricing bites
   (S3 + RDS are both managed there), with its open POSIX-lock bug as the standing caveat.

**How to apply:**
- Do NOT invest in making the staging CoW daemon / NFS export highly available. The staging SPOF costs
  developer time, not availability, and staging has a known end date.
- PREFERRED play for the staging SPOF: point staging at a small FSx (ONTAP or OpenZFS) filesystem and
  flip `clone_backend` off `"cow-daemon"`. Kills the SPOF, exercises the production code path, needs no
  new code, and REDUCES the staging/production divergence that
  [[project_verify_both_staging_environments]] exists to guard against.
- Known EKS-migration work items, worth flagging when touching the relevant code:
  SQLite-on-network-FS gets WORSE (cloud latency > LAN latency — strengthens the case for node-local
  `chunks.db` or RDS); node-identity assumptions need review for ephemeral pods (`ShardOwnership`,
  `executing_node`-scoped `cleanup_orphaned_jobs_on_startup`, node-local `DeploymentLock`, per-worker
  `HNSWIndexCache`); the entire auto-updater subsystem becomes dead weight and collapses into a
  Dockerfile (a large net simplification — see
  [[feedback_bootstrap_changes_need_installer_and_autoupdater]], whose whole mandate disappears in EKS);
  Bug #1538's attribute-cache freshness problem PERSISTS on cloud NFS, so the inode-fingerprint check
  stays load-bearing.
- NFSv3 remains pinned regardless of destination — that prohibition is now recorded in the project
  CLAUDE.md ("Shared-Storage Protocol Is Pinned to NFSv3 — NFSv4 Is Off The Table").
