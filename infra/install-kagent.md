```bash
KAGENT_DEFAULT_MODEL_PROVIDER=ollama kagent install --profile minimal
```



Expose kagent-ui 
```bash
kubectl port-forward \
  --address 0.0.0.0 \
  -n kagent \
  svc/kagent-ui \
  8088:8080
```