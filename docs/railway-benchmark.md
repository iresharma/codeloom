# Railway benchmark deployment

Project: https://railway.com/project/004efac5-5901-4e79-a995-46661838f9aa

- Production environment: `911a65b5-db92-4b78-8b0b-bce83506e157`.
- Docker-enabled x86-64 worker sandbox: `b7e915e2-9e48-443e-bfde-8951fc22d7b7`, idle destruction disabled.
- Monitor service: `b389235a-38a3-45d3-8af4-fb4c846a5b9f`.
- Monitor: https://benchmark-monitor-production.up.railway.app/.
- Persistent monitor volume: `0fa63413-333d-499a-8d53-ab030c91b4c7`, mounted at `/data`.
- Worker checkout: `/root/codeloom/workspace/benchmark`; engine sibling pinned to `fe6b2cb4779690855cad3e955c1e08e493847ab7`.
- Source branches: benchmark `codex/railway-benchmark`, engine `codex/railway-benchmark-runtime`, pages `codex/railway-live-results` (now static).

The owner authorized Railway execution on 2026-10-14 and requested a summary before paid runs. The official campaign waits for `.cache/cloud/paid-start-authorized`; never create it before delivering the current protocol/budget summary. No official calibration or measured attempts have started at this deployment checkpoint.

The separately authorized light smoke task is `dev-smoke-python`: add one read-only method to TinyDB. Its two systems ran concurrently outside v1 and outside the calibration suite. mini-SWE-agent passed; Engine stopped at its conservative $0.50 reservation cap while surveying, with no submitted patch. Recorded micro-dollar charges: Engine $0.019281, mini $0.010835, total $0.030116. Both outcomes, provider journals, patches and grader output are retained in `runs/railway-smoke` and published on the monitor. These development charges remain in the shared campaign ledger.

The owner changed the policy after the smoke: stop subsequent calls when **actual provider charges** reach a trial/stage/campaign target. Reject a call pre-emptively only when its conservative estimate exceeds **3x the full trial target**, never merely because it exceeds remaining headroom. The final admitted call may overshoot the target. Targets remain development $8, measured $32, recovery $5, fees/buffer $5; per measured attempt $1 light, $2 medium, $4 heavy (call gates $3/$6/$12). The $50 allocation is no longer a guaranteed hard ceiling. Both arms use the same policy. Within a trial, inference calls wait for the prior bill; the two arms still run concurrently. Unknown bills and provider/identity violations halt admission. Historical smoke results retain their original strict-reservation outcome. The existing ledger is migrated explicitly before calibration, without deleting or resetting charges; policy and multiplier are then frozen for resume.

To inspect the worker, use `railway sandbox exec` with the project/environment/sandbox IDs above. Its credentials and Git helper are host-only, outside task containers; never print or commit them. `.cache/campaign/ledger.sqlite` must never be deleted or reset. Full journals and a consistent SQLite backup are retained privately on the monitor; `/api/backup` requires the upload credential and is not under the public artifact root.

The owner removed task wall-clock watchdogs for both arms: `watchdog_s: null` in smoke, calibration, measurement and recovery. Controllers wait for task completion without an elapsed-time deadline; mini containers sleep indefinitely until the parent seals and removes them. Actual-spend targets and the 3x call-estimate gates still control inference admission. Per-request/network-stall and native tool-command timeouts remain independent of cumulative task duration. The earlier `railway-smoke-actual-spend` Engine run retains its historical 600-second watchdog outcome and recovered unmerged patch; removing the timer does not rerun or reinterpret that evidence.

After a settled smoke test, `python3 /root/start-benchmark.py --resume-setup` resumes CPU-only setup. It refuses unsettled bills and prior official trials. The supervisor then waits at the summary marker before paid calibration; failed certification, compatibility, calibration, pricing, identity or usage gates stop progression.

The main site at https://codeloom.iresharma.com/engine/results deliberately uses static imported data, with a link to the separate monitor. Once the selected campaign finishes, import its evidence with the documented benchmark `import-site` command, build and push `iresharma/codeloom-web:latest` for AMD64/ARM64, then restart `deployment/codeloom-web` in namespace `iresharma`. The cluster API is LAN-only. The static image deployed in this task is also tagged `railway-static-f1b6b74` (manifest digest `sha256:950b873d256ba054ae676fc4bee9fa5a2413acab70133ed90bb34c020b468a24`).

Railway infrastructure is charged separately from inference. The sandbox remains billable until destroyed; retain needed results/checkpoints first. A temporary full-site mirror (`fbd633b8-0b42-4ee3-bc46-c84d5dc06987`) remains because its deletion was not approved. It has no evidence volume and is not needed for the chosen static-site architecture.
