import assert from "node:assert/strict";
import test from "node:test";

import config, { partial } from "./railway.ts";

test("the cache volume and sleep setting remain attached to one replica", async () => {
  const { resources } = await config();
  const service = resources.find((resource) => resource.type === "service");
  assert.ok(service);
  assert.deepEqual(service.volumeAttachments["h1b-job-search-mcp-volume"], {
    volume: "volume.h1b-job-search-mcp-volume",
    mountPath: "/app/data_cache",
    volumeConfig: {
      region: "sfo",
      sizeMB: 5000,
      allowOnlineResize: true,
      alerts: { usage: { "80": {}, "95": {}, "100": {} } },
    },
  });
  assert.deepEqual(service.deploy.multiRegionConfig, { sfo: { numReplicas: 1 } });
  assert.equal(service.deploy.sleepApplication, true);
  assert.equal(service.deploy.restartPolicyMaxRetries, 3);
});

test("the partial leaves variables and other services unmanaged", async () => {
  const definition = await config();
  assert.equal(partial, "h1b-job-search-mcp");
  assert.deepEqual(definition.variables, { managed: false });
  assert.deepEqual(
    definition.resources.map((resource) => resource.address),
    ["service.h1b-job-search-mcp"],
  );
  assert.equal(definition.resources[0].variables, undefined);
});
