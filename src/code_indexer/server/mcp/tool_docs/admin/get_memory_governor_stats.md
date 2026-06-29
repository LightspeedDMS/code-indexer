---
name: get_memory_governor_stats
category: admin
required_permission: manage_users
tl_dr: Return the memory-governor band, pressure signal, counters, and config echoes.
slim_description: "Return the full memory-governor snapshot (band, used_pct, counters, watermark config)."
inputSchema:
  type: object
  properties: {}
  required: []
outputSchema:
  type: object
  properties:
    band:
      type: string
      enum:
      - GREEN
      - YELLOW
      - RED
      description: Current memory pressure band.
    used_pct:
      type: number
      description: Most-recently sampled used-memory percentage (0-100).
    effective_limit_mb:
      type: integer
      description: Effective memory limit in MiB (cgroup or host).
    effective_used_mb:
      type: integer
      description: Effective memory used in MiB.
    basis:
      type: string
      description: Source of the effective limit (cgroup_v2, cgroup_v1, or host).
    pswpin_rate:
      type: integer
      description: Swap-in page delta since the previous sample.
    swap_used_mb:
      type: number
      description: Total swap currently in use (MiB).
    green_to_yellow:
      type: integer
      description: Number of GREEN->YELLOW band transitions.
    yellow_to_red:
      type: integer
      description: Number of YELLOW->RED band transitions.
    red_to_yellow:
      type: integer
      description: Number of RED->YELLOW band transitions.
    yellow_to_green:
      type: integer
      description: Number of YELLOW->GREEN band transitions.
    shards_evicted_after_use:
      type: integer
      description: Number of shards evicted after use (RED action).
    lru_evictions:
      type: integer
      description: Number of LRU entries evicted (YELLOW proactive action).
    trim_calls:
      type: integer
      description: Number of malloc_trim calls.
    enabled:
      type: boolean
      description: Whether the memory governor is enabled.
    yellow_pct:
      type: number
      description: Configured YELLOW watermark percentage.
    red_pct:
      type: number
      description: Configured RED watermark percentage.
    hysteresis_pct:
      type: number
      description: Configured hysteresis gap percentage.
    red_min_dwell_seconds:
      type: number
      description: Minimum seconds to remain in RED before exit.
    sample_interval_seconds:
      type: number
      description: Sampler thread interval in seconds.
    swap_forces_red:
      type: boolean
      description: Whether swap-in activity forces the band to RED.
    rss_inflation_factor:
      type: number
      description: RSS inflation multiplier for LRU-cap calculations.
    pid:
      type: integer
      description: Process ID of the server process owning this governor.
    active:
      type: boolean
      description: Present and false only when the governor is not initialised.
  required:
  - enabled
---

TL;DR: Return the memory-governor band, pressure signal, counters, and config echoes.

USE CASES: (1) Check current memory pressure band (GREEN/YELLOW/RED) to understand cache eviction behaviour, (2) Inspect transition counters to diagnose oscillation, (3) Verify watermark config is correct after a Web UI change.

OUTPUT: Full snapshot dict with band, used_pct, effective_limit_mb, effective_used_mb, basis, pswpin_rate, swap_used_mb, flat transition/action counters, all watermark config echoes, and pid. When the governor is not initialised (CLI mode or pre-lifespan), returns {"enabled": false, "band": null, "active": false}.

PARAMETERS: None required.

RELATED TOOLS: check_health (overall server health), get_global_config (read/write config), admin_logs_query (query GOV-* structured log entries).
