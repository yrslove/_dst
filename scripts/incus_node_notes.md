# Incus NODE checklist

1. Linux host.
2. Install/configure Incus.
3. `incus admin init`.
4. Verify storage pool.
5. Verify bridge/network.
6. Verify GPU device available to host/container.
7. Create versioned base instance.
8. Install/configure graphical session.
9. Install Steam Linux client.
10. Manually verify DST.
11. Add agent.
12. Verify STOP/START persistence.
13. Only then use `RUNTIME_PROVIDER=incus`.

Recommended base strategy:

```text
dst-base-v1
  -> copy-on-write clone
     -> dst-000001
     -> dst-000002
```

Do not put reusable real account credentials into the base image.
