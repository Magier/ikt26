# ikt26

A [netshoot](https://github.com/nicolaka/netshoot) image with a small web
console, for poking at a Kubernetes cluster from inside a pod.

Type a command into the page, it runs in the container, you get the output.
Because the base is netshoot, `curl`, `dig`, `nmap`, `tcpdump`, `socat` and the
rest are already there.

## Use it

```sh
kubectl create namespace ikt
kubectl apply -f k8s/deployment.yaml
```

Then forward a port to it. Which form you need depends on where the browser is.

### From your own machine

```sh
kubectl -n ikt port-forward deploy/netshoot-console 8080:8080
```

Open <http://localhost:8080>.

### In an iximiuz Labs playground

Run the forward on `dev-machine`, and bind it to **all interfaces**:

```sh
kubectl -n ikt port-forward --address 0.0.0.0 deploy/netshoot-console 8081:8080
```

`--address 0.0.0.0` is the part that matters. `kubectl port-forward` listens on
`127.0.0.1` by default, and the lab's http-port proxy reaches `dev-machine` over
the network - a loopback-only listener is invisible to it, and the tab stays
blank.

Port **8081**, not 8080: the `ran-ui` service already holds 8080 on
`dev-machine`, and the playground already has a tab pointing at it.

Add a matching tab to the playground manifest:

```yaml
tabs:
  - id: http-port-console
    kind: http-port
    name: Console
    machine: dev-machine
    number: 8081
    access: private
    enabled: true
```

A tab proxies a port on a **machine**, not a Service or a pod - so the
port-forward (or a NodePort reachable from `dev-machine`) is what bridges the
cluster to the tab.

## API

The page is a form over the same thing you can call directly:

```sh
curl -sS localhost:8080/api/run -d '{"cmd": "dig +short payments-api.ikt"}'
```

```json
{
  "cmd": "dig +short payments-api.ikt",
  "output": "10.96.41.12\n",
  "exit": 0,
  "seconds": 0.021
}
```

`exit` is the command's exit status - a failed command is still a `200` with a
non-zero `exit`, not an HTTP error. A `400` means the request itself was wrong
(unparseable JSON, or no `cmd`). `GET /healthz` returns `ok` without shelling
out, which is what the probes use.

From inside the cluster, any pod can reach it at
`http://netshoot-console.ikt/api/run`.

## Build

Pushing to `main` builds `ghcr.io/magier/ikt26/netshoot-console:latest` for
amd64 and arm64 (`.github/workflows/image.yml`). A new GHCR package is
**private** even when the repository is public - flip it under
*Packages -> netshoot-console -> Package settings* or the pod will sit in
`ImagePullBackOff`.

Locally:

```sh
docker build -t netshoot-console .
docker run --rm -p 8080:8080 netshoot-console
```

## The obvious thing

The console executes whatever it is given, as root, with no authentication.
That is what it is for. Reach it through `kubectl port-forward` on a cluster you
own; do not put an Ingress or a LoadBalancer in front of it.

## Layout

```
app/server.py               the console - Python standard library only
Dockerfile                  netshoot, pinned by digest, + the console
k8s/deployment.yaml         Deployment + Service, no external exposure
.github/workflows/image.yml build and push to GHCR on every push to main
```
