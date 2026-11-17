import { defineRailway, github, project, service, volume } from "railway/iac";

// Scope ownership to this service and its cache volume.
export const partial = "h1b-job-search-mcp";

export default defineRailway(() => {
  const cacheVolume = volume("h1b-job-search-mcp-volume", {
    region: "sfo",
    sizeMB: 5000,
    allowOnlineResize: true,
    alerts: { usage: { "80": {}, "95": {}, "100": {} } },
  });
  const h1bJobSearchMcp = service("h1b-job-search-mcp", {
    source: github("awbjcj/h1b-job-search-mcp", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    healthcheck: "/health",
    healthcheckTimeout: 300,
    replicas: { "sfo": 1 },
    volumeMounts: { "/app/data_cache": cacheVolume },
    deploy: {
      sleepApplication: true,
      // Railway defaults to ON_FAILURE and omits that default from IaC state.
      restartPolicyMaxRetries: 3,
    },
  });

  return project("resume-tailor-harness", {
    // Service variables remain in Railway.
    variables: { managed: false },
    resources: [h1bJobSearchMcp],
  });
});
