# Railway infrastructure configuration

The named partial in `.railway/railway.ts` owns the H-1B service and its
existing `/app/data_cache` volume. It preserves the single replica, Dockerfile,
healthcheck, on-failure restart limit, and app sleep. Variables remain managed
in Railway. The résumé service in the same project is outside this partial.

To review and apply changes, install the pinned SDK with Node 22 or newer and
link the Railway CLI to the intended project, environment, and service:

```sh
npm ci --prefix .railway
railway link
railway config plan
railway config apply
```

Review the plan before applying it. Do not detach, recreate, or resize the
existing cache volume unless that change is intentional. A Git push does not
apply Railway IaC by itself; apply the reviewed configuration explicitly after
the change reaches the deployment branch. If using a different Railway project,
first adjust the project, repository, service, volume, and region in the file.

On Windows, if the SDK cannot find a sufficiently recent globally installed
Railway CLI, point `_` to its native executable for plan and apply:

```powershell
$env:_ = Join-Path ((npm root -g).Trim()) '@railway/cli/bin/railway.exe'
```

After deployment, check `/health` and a representative MCP query. The health
endpoint can report `degraded` while its data cache warms; see the
[memory and deployment notes](memory-architecture.md) for data verification.
