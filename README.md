# fleet-scenario-single

A single-container HTTP app for exercising Fleet's placement and autoscaling, and for asserting
what actually happened afterwards.

Covers **scenario 1** (one app, one instance, never scales) and **scenario 2** (one app, variable
traffic, autoscales to 3–4 instances).

Standard library only — no `pip install`, so the image builds in seconds, offline. That matters
when you are timing a scenario: a slow build must not be mistakable for slow placement.

## Endpoints

| endpoint | what it does | why it exists |
|---|---|---|
| `GET /healthz` | liveness | **required.** Fleet's drift-home refuses to migrate an app with no health check |
| `GET /` `GET /info` | who answered: hostname, pid, uptime, held MB, request count | proves *which replica* served, not just how many exist |
| `GET /mem?mb=N` | allocate and hold N MB (pages touched, so RSS really rises) | **the autoscale lever** |
| `GET /mem/release` | free it | return to baseline between runs |
| `GET /slow?ms=N` | hold the connection N ms | build queue depth without burning CPU |
| `GET /crash` | `exit(1)` after replying | prove the container restarts and traffic recovers |

## The one thing that will waste your afternoon

**CPU is not an autoscale signal.** Fleet scales on **memory** (as a percentage of the app's tier)
and on **request rate** (only when `target_rps` is set on the app). It never looks at CPU.

A load test that pegs the CPU will scale nothing, and the obvious conclusion — "autoscaling is
broken" — would be wrong. Drive `/mem`, or drive requests with `target_rps` configured.

## Deploying to Fleet

Create the app with a health check and a tier, then scale bounds:

```jsonc
{
  "id": "scn-single",
  "domain": "scn-single.apps.<your-base-domain>",
  "image": "ghcr.io/<you>/fleet-scenario-single:latest",
  "port": 8080,
  "tier": "micro",              // 256 MB — small tier makes /mem cross the threshold sooner
  "replicas": 1,
  "max_replicas": 4,
  "target_rps": 20,             // opt-in: without this, request rate is NOT a scale signal
  "health": { "path": "/healthz", "interval_s": 5, "timeout_s": 3,
              "unhealthy_after": 2, "healthy_after": 1 }
}
```

## Scenario 1 — steady, single instance

Deploy with `replicas: 1`, `max_replicas: 1`. Assert:

- the public URL returns `200`
- `/info.hostname` is **stable** across many requests (one replica, one node)
- `GET /crash`, then the URL returns `200` again within the restart window

## Scenario 2 — autoscale to 3–4 instances

Deploy as above with `max_replicas: 4`, then push it over a threshold:

```sh
# memory route — the reliable one
curl "$URL/mem?mb=200"        # ~78% of a 256 MB micro tier

# request-rate route — only works if target_rps is set
while true; do curl -s -o /dev/null "$URL/"; done
```

Assert:

- **distinct** `/info.hostname` values appear — Fleet places at most one replica per node, so
  scale-out is only real when more than one host answers
- replicas land on **different nodes** (`placed_nodes` in the API grows)
- if donors run out, a cloud node is provisioned and the extra replica lands there

Counting replicas in the API tells you what the control plane *intended*. Collecting hostnames
from `/info` tells you what is actually serving. Assert the second.

## Running locally

```sh
docker build -t scn-single .
docker run --rm -p 8080:8080 scn-single
curl localhost:8080/healthz
curl "localhost:8080/mem?mb=64" && curl localhost:8080/info
```
