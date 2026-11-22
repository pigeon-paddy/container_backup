# Container Backup

This app executes universal docker volume backups by doing cold backups and automatically starting/stopping the container. After a backup the return codes are send via webhook to trigger external script/bot.

It includes a stack.yml to run it as a swarm on remote devices.

## Use

### Compose
- Backup jobs on host device

```bash
docker compose build
docker compose create                     # create the containers (again after every change)
docker start -a backup-mc3
docker compose run --rm backup_mc3 <repr> <container_name> <container_path> [options]
```

### Swarm
Two one-shot backup jobs, one per machine, so each job controls the containers on ITS OWN Docker daemon.
Data is read locally on both machines; NFS mount is optional

1) Build the image on BOTH nodes
```bash
docker build -t container-backup:latest .
docker -H ssh://patrick@elitedesk build -t container-backup:latest .
```
    
2) Deploy (from the manager)
```bash
docker stack deploy --resolve-image never --prune -c stack.yml container-backup
```


3) create a context for all remote machines
```bash
docker context create Elitedesk --docker "host=ssh://<user>@<host>"   # make sure to add an ssh key to host and client first
docker --context Elitedesk ps
```
     
3) Run a backup - pick the swarm context by the machine the server runs on
```bash
docker service update --args "<repr> <container_name> <container_path> [options]" --force container-backup_<context>
```




| commmand    | description |
| ----------- | ----------- |
| repr   | Short server name, e.g. mc3_rs. Backups go to <backup-root>/<repr>/ and are named <repr>_<YYYY-mm-dd_HH-MM-SS>.tar.gz    |
| container_name | Docker container to stop/start, e.g. mc-backend-rs    |
| container_path    | Server data directory as seen inside THIS container, e.g. /servers/data_rs    |



Environment variables (all optional, flags override them):
- BACKUP_ROOT (/backup)
- KEEP (7)
- STOP_TIMEOUT (60)
- WEBHOOK_URL (n8n webhook)
- DOCKER_SOCKET (/var/run/docker.sock)

## JSON return codes

### state_backup
- 0: unsuccessfull backup
- 1: successfull backup

### state_container
- 0: Container wasn't running -> not started
- 1: Container was running -> started