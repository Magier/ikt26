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
kubectl -n ikt port-forward deploy/netshoot-console 8080:8080
```

Then open <http://localhost:8080>.

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
