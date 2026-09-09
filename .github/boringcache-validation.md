# py-lintro: BoringCache validation

Completed 9 September 2026. Fork-only validation; no outreach, upstream PR or comment was sent.

Both providers restored 1,513 C-cache hits and two misses on the seed revision. The warm workload timers exclude restoration and cannot establish a backend-caused compilation speed difference. GitHub's warm archive restore step took about 4s; BoringCache reported 17.5s and its complete setup step took 21s. The potential fit is retention outside the prospect's GitHub Docker cache budget. This short experiment does not demonstrate eviction under pressure or retention over days.

## Measured workloads

Timings measure the selected build command. Cold/warm values have one sample per phase and provider. Rolling values are the median and range across five different upstream revisions. Queue time, setup, cache restoration and post-job saving are outside the workload timer; these steps are available in the JSON evidence.

| Workload | Provider | Cold | Fresh-runner warm | Five rolling changes: median (range) |
| --- | --- | ---: | ---: | ---: |
| nuitka | BoringCache | 2028.6 | 438.1 | 404.0 (380.4–487.4) |
| nuitka | GitHub | 1903.4 | 539.8 | 384.0 (313.3–448.8) |

Qualification source: [upstream request](https://github.com/lgtm-hq/py-lintro/issues/2495).

macOS 15 arm64, Python 3.14.7, uv 0.12.11 and Nuitka 4.1.3. The upstream macOS binary build, binary verification and smoke checks passed. The compared cache surface is .nuitka-cache only; the separate unmerged optimization PR is outside this captured source.

## Source and integration

The captured source window starts at `e8773c533caff41403dc40f37eee608c20143f01` (captured upstream head ~5) and ends at `5d5572a22cecd58f2417057299f380b06871b56a`. Every adjacent first-parent patch was applied in order. Each job checks source equality against `.github/boringcache-source`, excluding only the validation harness.

The paired jobs use the same source, runner class, toolchain and cache surface. BoringCache One is pinned to `404b744a2053da4cf963f13f615f7fafe94f3cf7` (v1.30.1); actual CLI versions are retained. Authentication uses GitHub OIDC, with no static cache token. Cold and rolling jobs may publish; warm jobs restore only. Rust jobs use sccache 0.17.0 for both providers and no target/package cache. The cc crate can also use the Rust wrapper for native dependencies; Rust counts are reported separately.

| Change | Upstream revision | Subject |
| ---: | --- | --- |
| 1 | [cbe03f587f89](https://github.com/lgtm-hq/py-lintro/commit/cbe03f587f897109b0fd8bea765118bab176024c) | chore(deps): update rust-lang/rust to 1.98.1 (#2465) |
| 2 | [af2d97bfde1f](https://github.com/lgtm-hq/py-lintro/commit/af2d97bfde1f075f0cb05c88a45068d3fac12c8d) | chore(deps): renovate bump (#2486) |
| 3 | [e8d2293ad250](https://github.com/lgtm-hq/py-lintro/commit/e8d2293ad2509d5b647077fcaf7fa3609ad9705f) | fix(ci): pin the lintro-tools image built from main's rustc 1.98.1 manifest (#2499) |
| 4 | [b0cf8ad21dc7](https://github.com/lgtm-hq/py-lintro/commit/b0cf8ad21dc72a7ffface2b3a9ce5e7138b7eaf9) | chore(release): version 0.152.4 (#2500) |
| 5 | [5d5572a22cec](https://github.com/lgtm-hq/py-lintro/commit/5d5572a22cecd58f2417057299f380b06871b56a) | ci: allow the hosted-compute watchdog hosts on the linux binary job (#2487) |

## Per-job evidence

[Measurements](boringcache-measurements.json) retain source hashes, timings, commands, test outcomes, native counters and final job links. [Source window](boringcache-source-window.json) retains the original revisions and changed paths. Actions artifacts have a 30-day retention setting; these committed summaries do not depend on artifact retention.

| Source index | Case / phase | Provider | Workload seconds | Job |
| ---: | --- | --- | ---: | --- |
| 0 | nuitka / cold | BoringCache | 2028.578 | [job 102463534562](https://github.com/boringcache/py-lintro/actions/runs/34350862282/job/102463534562) |
| 0 | nuitka / cold | GitHub | 1903.405 | [job 102463534650](https://github.com/boringcache/py-lintro/actions/runs/34350862282/job/102463534650) |
| 0 | nuitka / warm | GitHub | 539.758 | [job 102474824868](https://github.com/boringcache/py-lintro/actions/runs/34350862282/job/102474824868) |
| 0 | nuitka / warm | BoringCache | 438.119 | [job 102474824937](https://github.com/boringcache/py-lintro/actions/runs/34350862282/job/102474824937) |
| 1 | nuitka / commit | GitHub | 442.972 | [job 102478389451](https://github.com/boringcache/py-lintro/actions/runs/34355317864/job/102478389451) |
| 1 | nuitka / commit | BoringCache | 404.003 | [job 102478390189](https://github.com/boringcache/py-lintro/actions/runs/34355317864/job/102478390189) |
| 2 | nuitka / commit | GitHub | 383.952 | [job 102481345011](https://github.com/boringcache/py-lintro/actions/runs/34356186799/job/102481345011) |
| 2 | nuitka / commit | BoringCache | 487.390 | [job 102481345337](https://github.com/boringcache/py-lintro/actions/runs/34356186799/job/102481345337) |
| 3 | nuitka / commit | BoringCache | 391.109 | [job 102484847123](https://github.com/boringcache/py-lintro/actions/runs/34357219189/job/102484847123) |
| 3 | nuitka / commit | GitHub | 313.342 | [job 102484847473](https://github.com/boringcache/py-lintro/actions/runs/34357219189/job/102484847473) |
| 4 | nuitka / commit | GitHub | 370.990 | [job 102487631697](https://github.com/boringcache/py-lintro/actions/runs/34358048705/job/102487631697) |
| 4 | nuitka / commit | BoringCache | 450.034 | [job 102487632012](https://github.com/boringcache/py-lintro/actions/runs/34358048705/job/102487632012) |
| 5 | nuitka / commit | GitHub | 448.830 | [job 102491064878](https://github.com/boringcache/py-lintro/actions/runs/34359061982/job/102491064878) |
| 5 | nuitka / commit | BoringCache | 380.401 | [job 102491065291](https://github.com/boringcache/py-lintro/actions/runs/34359061982/job/102491065291) |


## Full integration follow-up

The [Cargo/Docker integration report](boringcache-full-validation.md) records the subsequent first-class Cargo and relevant Docker validation. The compiler/archive results above remain a separate cohort; use the preserved `compiler-cache-validation` branch to reproduce that configuration.
