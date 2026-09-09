# py-lintro: Cargo and Docker integration validation

Measured on 9 September 2026. This fork follows the [adorsys status-list-server integration](https://github.com/boringcache/status-list-server/tree/refs/heads/boringcache-validation): a repository-owned `.boringcache.toml` plan, thin One workflow steps, OIDC authentication, trusted writers and read-only consumers.

The runs use released One/CLI 1.30.1 and sccache 0.17.0. One is pinned to `404b744a2053da4cf963f13f615f7fafe94f3cf7`. No unreleased CLI changes were used. Whole-job times exclude queue time; One elapsed includes its own cache operations and wrapped command. These timing boundaries must not be mixed.

The existing Python/Nuitka archive remains configured separately. Its tools image genuinely compiles Rust when its prebuilt-tool download path is unavailable: cargo-deny and taplo fell back to `cargo install` in the observed builds. The integration preserves that prebuilt preference and does not manufacture extra compilation. The Dockerfile installs checksum-verified sccache 0.17.0, retains the existing apt, Bun, Cargo registry/Git and uv cache mounts, and retains the existing tool checks.

In the [ordinary Docker pair](https://github.com/boringcache/py-lintro/actions/runs/34362764988) at `7adb76e78af95704d5dce7a87cc79bc6b53cfce5`, One elapsed was 489.8 s seed and 88.6 s warm. The warm build reused 17 vertices and loaded the same image ID, `sha256:d8e965a119efa9c8b85fdf40e086360ac8227b79d26059678cb91f723da929aa`, with reported image size 4,865,182,843 bytes. This size is a Docker image measurement, not transferred or uniquely retained cache bytes. Both jobs verified the loaded tools.

The follow-up Bake configuration supports a controlled `REBUILD_TOOLS=true` diagnostic that bypasses normal layer reuse. The [first diagnostic](https://github.com/boringcache/py-lintro/actions/runs/34364191604) passed application verification but did not demonstrate cache reuse: moving from `buildx build` to Bake selected a new target-qualified namespace. All six mount lookups missed; native sccache recorded zero hits, 436 misses and 436 write errors in the read-only consumer. This attempt is excluded from reuse claims. A matched writer/reader pair now seeds that exact Bake identity before the forced rebuild.

The [matched Docker compiler/mount diagnostic](https://github.com/boringcache/py-lintro/actions/runs/34366355087) passed both jobs at `6d2d01bad040c328f6867ceee3383ba69a636fd9`. The seed took 549 s and the read-only rebuild 475 s; One elapsed was 530.6 s and 458.0 s. Both deliberately bypassed ordinary layer reuse, so these timings are separate from the ordinary Docker pair above.

The rebuild restored four mounts: `/var/cache/apt` in 0.772 s, `/var/lib/apt` in 0.899 s, `/opt/cargo/registry` in 4.855 s and `/root/.cache/uv` in 4.711 s. The Bun and Cargo Git mounts missed; no reuse is claimed for them. sccache served 413 hits: 316 Rust, 69 C/C++ and 28 assembler. There were 23 Rust misses, giving 93.22% Rust reuse and 94.72% overall reuse. Native read errors and cache errors were zero; the read-only consumer recorded 23 write errors corresponding in count to its misses. No raw debug trace was retained to assign an HTTP cause to those write errors. The trusted writer recorded zero write/read/cache errors. Cargo-deny compilation fell from 129 s to 84 s, and taplo from 129 s to 38.90 s. Image loading, package installation and other work still account for much of the whole job.


## Evidence and limits

The [full integration measurements](boringcache-full-measurements.json) retain workflow and job URLs, exact configuration SHAs, step timings, native tool statistics and relevant log observations. Failed and canceled attempts remain in the evidence. Successful job status alone is not treated as proof of complete cache reuse or zero cache errors.

The earlier [compiler/archive comparison](https://github.com/boringcache/py-lintro/blob/compiler-cache-validation/.github/boringcache-validation.md) starts at the captured upstream HEAD~5 and tests five real first-parent changes. It is a separate cohort and must not be relabeled as full Cargo/Docker measurements. Its historical workflows should be dispatched from `compiler-cache-validation`; their strict source-equality checks intentionally reject later integration changes. The captured source window remains in `boringcache-source-window.json` beside this report.

These observations do not establish long-term retention, eviction resilience, unique-storage or cost savings, or a matched full-pipeline speed improvement over upstream. No upstream pull request, GitHub comment or outreach message was sent by this validation task.
